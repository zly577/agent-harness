"""Read user overrides without changing the shared environment settings."""
import json

from app.core.config import settings
from app.db.models import UserPreferences
from app.db.session import SessionLocal
from app.schemas.preferences import AgentPreferences


def preference_defaults() -> dict:
    return {name: getattr(settings, name) for name in AgentPreferences.model_fields}


def preferences_for(db, user_id: str | None) -> dict:
    values = preference_defaults()
    row = db.get(UserPreferences, user_id) if user_id else None
    if row:
        overrides = json.loads(row.values_json)
        values.update({key: value for key, value in overrides.items() if key in values})
    return values


def load_preferences(user_id: str | None) -> dict:
    with SessionLocal() as db:
        return preferences_for(db, user_id)
