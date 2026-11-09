"""Single-worker scheduler. MySQL is the source of truth for due times and history."""
import asyncio
import logging
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select, update

from app.agent import runtime
from app.agent.approvals import read_pending_approval
from app.agent.runner import SessionRun, run_manager
from app.core.crypto import scrub_secrets
from app.db.models import LlmProvider, ScheduledRun, ScheduledTask, User, Workspace
from app.db.models import Session as DbSession
from app.db.session import SessionLocal
from app.services.chat import PreparedChat, start_chat
from app.services.schedule_rules import (
    ACTIVE_RUN_STATUSES,
    active_run_exists,
    lock_schedule,
    next_occurrence,
    utcnow,
    workspace_directory_available,
)

logger = logging.getLogger(__name__)
_POLL_SECONDS = 5
_GRACE = timedelta(minutes=2)
_BATCH_SIZE = 32
_MAX_RUNNING = 4
_PENDING = ("awaiting_approval", "awaiting_input")
_RUN_STATUS = {
    "running": "running",
    "done": "succeeded",
    "error": "failed",
    "stopped": "stopped",
    "awaiting_approval": "awaiting_approval",
    "awaiting_input": "awaiting_input",
}


def _record_due(db, task: ScheduledTask, due: datetime, now: datetime, status: str, reason: str = "") -> None:
    existing = db.scalar(select(ScheduledRun.id).where(
        ScheduledRun.task_id == task.id, ScheduledRun.scheduled_for == due,
    ).with_for_update())
    if existing is not None:
        # Recomputing a plan after a clock change can revisit an already recorded instant.
        return
    db.add(ScheduledRun(
        id=uuid4().hex,
        task_id=task.id,
        trigger_type="scheduled",
        scheduled_for=due,
        status=status,
        reason=reason,
        created_at=now,
        finished_at=now if status != "queued" else None,
    ))


def collect_due(now: datetime) -> None:
    """Record a due occurrence and advance its schedule in the same short transaction."""
    with SessionLocal() as db:
        ids = db.scalars(
            select(ScheduledTask.id)
            .where(ScheduledTask.enabled.is_(True), ScheduledTask.next_run_at <= now)
            .order_by(ScheduledTask.next_run_at, ScheduledTask.id)
            .limit(_BATCH_SIZE)
        ).all()
    for task_id in ids:
        with SessionLocal.begin() as db:
            task = lock_schedule(db, task_id)
            if not task or not task.enabled or task.next_run_at is None or task.next_run_at > now:
                continue
            due = task.next_run_at
            cutoff = now - _GRACE
            if due < cutoff:
                _record_due(db, task, due, now, "missed", "已超过补跑时限；该时段内的错过执行已合并记录")
                due = next_occurrence(task, cutoff - timedelta(microseconds=1))
            if due is not None and due <= now:
                if active_run_exists(db, task.id):
                    _record_due(db, task, due, now, "skipped", "上一次执行仍未结束")
                else:
                    _record_due(db, task, due, now, "queued")
            task.next_run_at = next_occurrence(task, now)


def _queued() -> list[tuple[str, str]]:
    with SessionLocal() as db:
        running = db.scalar(
            select(func.count()).select_from(ScheduledRun).where(ScheduledRun.status.in_(("starting", "running")))
        )
        capacity = _MAX_RUNNING - running
        if capacity <= 0:
            return []
        return list(db.execute(
            select(ScheduledRun.id, ScheduledRun.task_id)
            .where(ScheduledRun.status == "queued")
            .order_by(ScheduledRun.created_at, ScheduledRun.id)
            .limit(capacity)
        ).tuples())


def _claim(run_id: str, task_id: str, now: datetime) -> PreparedChat | None:
    """Persist the session and starting marker before any Agent work can begin."""
    with SessionLocal.begin() as db:
        task = lock_schedule(db, task_id)
        row = db.get(ScheduledRun, run_id, with_for_update=True)
        if task is None or row is None or row.status != "queued":
            return None
        if row.scheduled_for is not None and row.scheduled_for < now - _GRACE:
            row.status, row.reason, row.finished_at = "missed", "等待执行期间已超过补跑时限", now
            return None
        owner = db.get(User, task.user_id)
        workspace = db.get(Workspace, task.workspace_id) if task.workspace_id else None
        provider = db.get(LlmProvider, task.provider_id) if task.provider_id else None
        invalid_owner = owner is None or not owner.is_active
        invalid_workspace = task.workspace_id is not None and (
            workspace is None or workspace.user_id != task.user_id
            or not workspace_directory_available(workspace)
        )
        reason = ""
        if invalid_owner:
            reason = "任务所属账号不可用"
        elif invalid_workspace:
            reason = "任务工作区不存在或不可访问"
        elif task.provider_id and (provider is None or provider.user_id != task.user_id or not provider.enabled):
            reason = "任务指定的模型供应商不存在、已停用或不可访问"
        if reason:
            row.status, row.reason, row.finished_at = "failed", reason, now
            if invalid_owner or invalid_workspace:
                task.enabled, task.next_run_at = False, None
            return None

        session_id = uuid4().hex
        db.add(DbSession(
            id=session_id, user_id=task.user_id, workspace_id=task.workspace_id,
            provider_id=task.provider_id, title=task.title, status="active",
        ))
        db.flush()
        row.session_id, row.status, row.started_at = session_id, "starting", now
        return PreparedChat(session_id, task.user_id, task.prompt, task.prompt)


def _still_claimed(session_id: str) -> bool:
    with SessionLocal() as db:
        return db.scalar(
            select(ScheduledRun.id)
            .where(ScheduledRun.session_id == session_id, ScheduledRun.status == "starting")
        ) is not None


@dataclass(frozen=True)
class _Status:
    session_id: str
    owner_id: str | None
    status: str
    at: datetime
    reason: str = ""


def _save_updates(updates: list[_Status]) -> None:
    with SessionLocal.begin() as db:
        for item in updates:
            allowed = ACTIVE_RUN_STATUSES
            if item.status == "running":
                allowed = ("starting", *_PENDING)
            db.execute(
                update(ScheduledRun)
                .where(ScheduledRun.session_id == item.session_id, ScheduledRun.status.in_(allowed))
                .values(
                    status=item.status,
                    reason=item.reason,
                    finished_at=None if item.status in ACTIVE_RUN_STATUSES else item.at,
                )
            )


def _recoverable() -> list[tuple[str, str]]:
    with SessionLocal() as db:
        return list(db.execute(
            select(ScheduledRun.session_id, ScheduledTask.user_id)
            .join(ScheduledTask, ScheduledTask.id == ScheduledRun.task_id)
            .where(ScheduledRun.session_id.is_not(None), ScheduledRun.status.in_(("starting", "running", *_PENDING)))
        ).tuples())


async def _pending_status(session_id: str, owner_id: str | None) -> str | None:
    agent = await runtime.get_agent(session_id, user_id=owner_id, history_only=True)
    pending = await read_pending_approval(agent, {"configurable": {"thread_id": session_id}}, user_id=owner_id)
    if pending and pending.get("pending"):
        return "awaiting_input" if pending.get("clarifications") else "awaiting_approval"
    return None


class Scheduler:
    """Dispatch and observe scheduled runs on the same loop as the chat runner."""

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._updates: deque[_Status] = deque()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False

    async def start(self) -> None:
        for session_id, owner_id in await asyncio.to_thread(_recoverable):
            status = await self._read_pending(session_id, owner_id)
            self._active.add(session_id)
            self._updates.append(_Status(
                session_id, owner_id, status or "interrupted", utcnow(),
                "" if status else "服务在执行期间重启，结果未确认",
            ))
        await self.flush()
        run_manager.set_status_observer(self._observe)
        self._task = asyncio.create_task(self._loop())

    async def stop_dispatching(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            await self._task

    async def close(self) -> None:
        run_manager.set_status_observer(None)
        try:
            await self.flush()
        except Exception:
            logger.exception("Could not persist scheduled run statuses at shutdown")

    def _observe(self, run: SessionRun) -> None:
        if run.session_id not in self._active:
            return
        status = _RUN_STATUS[run.status]
        reason = run.error
        if status == "stopped" and self._stopping:
            status, reason = "interrupted", "服务已停止，本次执行中断"
        at = datetime.fromtimestamp(run.finished_at or run.started_at, UTC).replace(tzinfo=None)
        self._updates.append(_Status(run.session_id, run.user_id, status, at, reason))
        self._wake.set()

    async def _read_pending(self, session_id: str, owner_id: str | None) -> str | None:
        try:
            return await _pending_status(session_id, owner_id)
        except Exception:
            logger.warning("Unable to recover scheduled checkpoint for session %s", session_id, exc_info=True)
            return None

    async def flush(self) -> None:
        if not self._updates:
            return
        pending_updates = list(self._updates)
        updates = []
        for item in pending_updates:
            # A failed resume can leave the original interrupt in the checkpoint.
            if item.status == "failed":
                pending = await self._read_pending(item.session_id, item.owner_id)
                current = run_manager.current(item.session_id)
                if current and datetime.fromtimestamp(current.started_at, UTC).replace(tzinfo=None) > item.at:
                    # The owner retried while we were reading the checkpoint.
                    continue
                if pending:
                    item = replace(item, status=pending)
            updates.append(item)
        await asyncio.to_thread(_save_updates, updates)
        for _ in pending_updates:
            self._updates.popleft()
        for item in updates:
            if item.status not in ACTIVE_RUN_STATUSES:
                self._active.discard(item.session_id)

    def _start_claimed(self, chat: PreparedChat) -> None:
        # Deletion routes can remove the just-claimed session while the loop
        # awaits the transaction. Check without yielding before reserving it.
        try:
            if not _still_claimed(chat.session_id):
                return
            self._active.add(chat.session_id)
            if not self._stopping:
                start_chat(chat)
                return
            status, reason = "interrupted", "服务在启动任务前停止"
        except Exception as exc:
            status, reason = "failed", scrub_secrets(str(exc))
        self._updates.append(_Status(chat.session_id, chat.user_id, status, utcnow(), reason))
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                await self.flush()
                if self._stopping:
                    break
                await asyncio.to_thread(collect_due, utcnow())
                for run_id, task_id in await asyncio.to_thread(_queued):
                    if self._stopping:
                        break
                    chat = await asyncio.to_thread(_claim, run_id, task_id, utcnow())
                    if chat is not None:
                        self._start_claimed(chat)
            except Exception:
                logger.exception("Scheduled task dispatch cycle failed")
            if not self._stopping:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=_POLL_SECONDS)
                except TimeoutError:
                    pass
