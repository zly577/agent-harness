"""Manage a user's schedules and browse their durable execution history."""
from datetime import datetime, time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.providers import get_owned_provider
from app.api.deps import get_current_user
from app.db.models import ScheduledRun, ScheduledTask, User, Workspace
from app.db.session import get_db
from app.schemas.schedules import ScheduleCreate, ScheduleUpdate, ScheduledRunView, ScheduledTaskView
from app.services.schedule_rules import (
    active_run_exists,
    load_timezone,
    local_to_utc,
    lock_schedule,
    next_occurrence,
    utcnow,
    workspace_directory_available,
)

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])

_NOT_FOUND = "定时任务不存在"
_TIMING_FIELDS = {"timezone", "frequency", "time_of_day", "weekdays", "once_at"}
_DETAIL_FIELDS = {"title", "prompt", "workspace_id", "provider_id"}


def _owned_locked(db: Session, task_id: str, user: User) -> ScheduledTask:
    task = lock_schedule(db, task_id, user.id)
    if task is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return task


def _validate_references(db: Session, task: ScheduledTask) -> None:
    if task.workspace_id:
        workspace = db.get(Workspace, task.workspace_id)
        if workspace is None or workspace.user_id != task.user_id:
            raise HTTPException(status_code=404, detail="工作区不存在，请重新选择")
        if not workspace_directory_available(workspace):
            raise HTTPException(status_code=409, detail="工作区目录不存在或不可访问，请重新选择")
    if task.provider_id:
        provider = get_owned_provider(db, task.provider_id, task.user_id)
        if provider is None or not provider.enabled:
            raise HTTPException(status_code=404, detail="模型供应商不存在或已停用，请重新选择")


def _apply_timing(task: ScheduledTask, values: dict, now: datetime) -> None:
    task.timezone = values.get("timezone", task.timezone)
    task.frequency = values.get("frequency", task.frequency)
    zone = load_timezone(task.timezone)
    if task.frequency == "once":
        if "once_at" in values:
            local = values["once_at"]
            task.once_at = local_to_utc(datetime.fromisoformat(local), zone) if local else None
            if local and task.once_at is None:
                raise ValueError("该时区没有这个本地时间，请选择另一个时间")
        if task.once_at is None or task.once_at <= now:
            raise ValueError("单次执行时间必须晚于当前时间")
        task.time_of_day = None
        task.weekdays = []
    else:
        if "time_of_day" in values:
            local_time = values["time_of_day"]
            task.time_of_day = time.fromisoformat(local_time) if local_time else None
        if task.time_of_day is None:
            raise ValueError("请选择每天或每周的执行时间")
        task.weekdays = sorted(set(values.get("weekdays", task.weekdays))) if task.frequency == "weekly" else []
        if task.frequency == "weekly" and not task.weekdays:
            raise ValueError("每周任务至少需要选择一天")
        task.once_at = None
    task.next_run_at = next_occurrence(task, now) if task.enabled else None


def _task_view(task: ScheduledTask, latest: ScheduledRun | None = None) -> ScheduledTaskView:
    view = ScheduledTaskView.model_validate(task)
    if latest is not None:
        view.latest_run = ScheduledRunView.model_validate(latest)
    return view


def _run_history(task_id: str):
    return (
        select(ScheduledRun).where(ScheduledRun.task_id == task_id)
        .order_by(ScheduledRun.created_at.desc(), ScheduledRun.id.desc())
    )


def _commit_task(db: Session, task: ScheduledTask) -> ScheduledTaskView:
    latest = db.scalar(_run_history(task.id).limit(1))
    response = _task_view(task, latest)
    db.commit()
    return response


@router.get("", response_model=list[ScheduledTaskView])
def list_schedules(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    latest_id = (
        select(ScheduledRun.id).where(ScheduledRun.task_id == ScheduledTask.id)
        .order_by(ScheduledRun.created_at.desc(), ScheduledRun.id.desc()).limit(1)
        .correlate(ScheduledTask).scalar_subquery()
    )
    rows = db.execute(
        select(ScheduledTask, ScheduledRun).outerjoin(ScheduledRun, ScheduledRun.id == latest_id)
        .where(ScheduledTask.user_id == user.id)
        .order_by(ScheduledTask.created_at.desc(), ScheduledTask.id.desc())
    )
    return [_task_view(task, latest) for task, latest in rows]


@router.post("", response_model=ScheduledTaskView, status_code=201)
def create_schedule(req: ScheduleCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = utcnow()
    task = ScheduledTask(
        id=uuid4().hex, user_id=user.id, enabled=True, created_at=now, updated_at=now,
        title=req.title, prompt=req.prompt, workspace_id=req.workspace_id, provider_id=req.provider_id,
    )
    _validate_references(db, task)
    try:
        _apply_timing(task, req.model_dump(), now)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add(task)
    db.commit()
    return _task_view(task)


@router.patch("/{task_id}", response_model=ScheduledTaskView)
def update_schedule(task_id: str, req: ScheduleUpdate, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    task = _owned_locked(db, task_id, user)
    values = req.model_dump(exclude_unset=True)
    for name in _DETAIL_FIELDS & values.keys():
        setattr(task, name, values[name])
    _validate_references(db, task)
    now = utcnow()
    if _TIMING_FIELDS & values.keys():
        try:
            _apply_timing(task, values, now)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if values:
        task.updated_at = now
    return _commit_task(db, task)


@router.post("/{task_id}/pause", response_model=ScheduledTaskView)
def pause_schedule(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = _owned_locked(db, task_id, user)
    if task.enabled:
        task.enabled = False
        task.next_run_at = None
        task.updated_at = utcnow()
    return _commit_task(db, task)


@router.post("/{task_id}/resume", response_model=ScheduledTaskView)
def resume_schedule(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = _owned_locked(db, task_id, user)
    _validate_references(db, task)
    if task.enabled and task.next_run_at is not None:
        return _commit_task(db, task)
    now = utcnow()
    next_run_at = next_occurrence(task, now)
    if next_run_at is None:
        raise HTTPException(status_code=409, detail="单次执行时间已过，请修改计划时间或立即运行")
    task.enabled = True
    task.next_run_at = next_run_at
    task.updated_at = now
    return _commit_task(db, task)


@router.post("/{task_id}/run-now", response_model=ScheduledRunView, status_code=202)
def run_schedule_now(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    task = _owned_locked(db, task_id, user)
    _validate_references(db, task)
    now = utcnow()
    active = active_run_exists(db, task.id)
    run = ScheduledRun(
        id=uuid4().hex, task_id=task.id, trigger_type="manual", status="skipped" if active else "queued",
        reason="已有一次执行正在进行或等待处理" if active else None,
        created_at=now, finished_at=now if active else None,
    )
    db.add(run)
    response = ScheduledRunView.model_validate(run)
    db.commit()
    return response


@router.get("/{task_id}/runs", response_model=list[ScheduledRunView])
def list_schedule_runs(task_id: str, limit: int = Query(default=50, ge=1, le=100),
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    owned_id = db.scalar(select(ScheduledTask.id).where(ScheduledTask.id == task_id, ScheduledTask.user_id == user.id))
    if owned_id is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return list(db.scalars(_run_history(task_id).limit(limit)))
