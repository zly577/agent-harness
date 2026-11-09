"""Schedule clock rules and database locks shared by the API and dispatcher."""
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models import ScheduledRun, ScheduledTask, Workspace

ACTIVE_RUN_STATUSES = ("queued", "starting", "running", "awaiting_approval", "awaiting_input")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def workspace_directory_available(workspace: Workspace) -> bool:
    try:
        return Path(workspace.directory).expanduser().resolve().is_dir()
    except (OSError, RuntimeError, ValueError):
        return False


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("请输入有效的 IANA 时区，例如 Asia/Shanghai") from exc


def local_to_utc(local: datetime, zone: ZoneInfo) -> datetime | None:
    """Resolve the first occurrence of a local time; a DST gap has no occurrence."""
    try:
        instant = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        valid = instant.astimezone(zone).replace(tzinfo=None) == local
    except OverflowError:
        return None
    return instant.replace(tzinfo=None) if valid else None


def next_occurrence(task: ScheduledTask, after: datetime) -> datetime | None:
    """Return the first occurrence strictly after the given naive UTC time."""
    if task.frequency == "once":
        return task.once_at if task.once_at and task.once_at > after else None

    zone = load_timezone(task.timezone)
    local_date = after.replace(tzinfo=UTC).astimezone(zone).date()
    # A weekly slot can be skipped by a DST transition, so allow a second week.
    for offset in range(15):
        if offset > (date.max - local_date).days:
            break
        day = local_date + timedelta(days=offset)
        if task.frequency == "weekly" and day.isoweekday() not in task.weekdays:
            continue
        candidate = local_to_utc(datetime.combine(day, task.time_of_day), zone)
        if candidate is not None and candidate > after:
            return candidate
    return None


def lock_schedule(db: Session, task_id: str, user_id: str | None = None) -> ScheduledTask | None:
    """Lock the task before checking or modifying its runs in the same transaction."""
    conditions = [ScheduledTask.id == task_id]
    if user_id is not None:
        conditions.append(ScheduledTask.user_id == user_id)
    if db.get_bind().dialect.name == "sqlite":
        # SQLite has no SELECT FOR UPDATE. An unchanged write obtains its write lock.
        db.execute(update(ScheduledTask).where(*conditions).values(id=ScheduledTask.id))
    return db.scalar(
        select(ScheduledTask).where(*conditions).with_for_update().execution_options(populate_existing=True)
    )


def active_run_exists(db: Session, task_id: str, *, exclude_run_id: str | None = None) -> bool:
    query = select(ScheduledRun.id).where(
        ScheduledRun.task_id == task_id, ScheduledRun.status.in_(ACTIVE_RUN_STATUSES)
    )
    if exclude_run_id is not None:
        query = query.where(ScheduledRun.id != exclude_run_id)
    # A locking read sees commits made after an earlier snapshot in MySQL's default isolation level.
    return db.scalar(query.limit(1).with_for_update()) is not None
