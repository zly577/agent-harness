"""Validation and UTC serialization for schedule APIs."""
from datetime import UTC, datetime, time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Frequency = Literal["once", "daily", "weekly"]
RunStatus = Literal[
    "queued", "starting", "running", "awaiting_approval", "awaiting_input", "succeeded",
    "failed", "stopped", "skipped", "missed", "interrupted",
]

Title = Annotated[str, Field(min_length=1, max_length=128)]
Prompt = Annotated[str, Field(min_length=1, max_length=8000)]
ResourceId = Annotated[str, Field(min_length=1, max_length=32)]
Timezone = Annotated[str, Field(min_length=1, max_length=128)]
_HOUR_MINUTE = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
LocalTime = Annotated[str, Field(pattern=rf"^{_HOUR_MINUTE}$")]
LocalDateTime = Annotated[str, Field(pattern=rf"^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T{_HOUR_MINUTE}$")]
Weekday = Annotated[int, Field(strict=True, ge=1, le=7)]
Weekdays = Annotated[list[Weekday], Field(max_length=7)]


class ScheduleFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: ResourceId | None = None
    provider_id: ResourceId | None = None
    time_of_day: LocalTime | None = None
    weekdays: Weekdays = Field(default_factory=list)
    once_at: LocalDateTime | None = None

    @field_validator("title", "prompt", check_fields=False)
    @classmethod
    def require_nonblank(cls, value: str, info) -> str:
        if not value.strip():
            raise ValueError("标题和任务内容不能为空")
        return value.strip() if info.field_name == "title" else value


class ScheduleCreate(ScheduleFields):
    title: Title
    prompt: Prompt
    timezone: Timezone
    frequency: Frequency


class ScheduleUpdate(ScheduleFields):
    title: Title | None = None
    prompt: Prompt | None = None
    timezone: Timezone | None = None
    frequency: Frequency | None = None

    @field_validator("title", "prompt", "timezone", "frequency", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("此字段不能为空")
        return value


def _aware_utc(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None else None


class ScheduledRunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    session_id: str | None
    trigger_type: Literal["scheduled", "manual"]
    scheduled_for: datetime | None
    status: RunStatus
    reason: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    _as_utc = field_validator("scheduled_for", "created_at", "started_at", "finished_at")(_aware_utc)


class ScheduledTaskView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    prompt: str
    workspace_id: str | None
    provider_id: str | None
    timezone: str
    frequency: Frequency
    time_of_day: str | None
    weekdays: list[int]
    once_at: datetime | None
    next_run_at: datetime | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    latest_run: ScheduledRunView | None = None

    _as_utc = field_validator("once_at", "next_run_at", "created_at", "updated_at")(_aware_utc)

    @field_validator("time_of_day", mode="before")
    @classmethod
    def format_local_time(cls, value):
        return value.isoformat(timespec="minutes") if isinstance(value, time) else value
