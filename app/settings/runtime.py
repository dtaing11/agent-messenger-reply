"""Runtime configuration: environment defaults overlaid with values saved in the portal.

Every request/job asks for a fresh snapshot (`effective_settings`), so a value saved in the
portal takes effect on the next webhook or queued event with no restart. A snapshot is an
immutable Settings object, so an in-flight job never sees a credential change halfway.
"""

import json
import sqlite3
from typing import Annotated, Any

from pydantic import SecretStr, TypeAdapter

from app.config import Settings
from app.settings.secret_store import SecretStore

# Credentials: stored encrypted, never shown again after saving.
SECRET_FIELDS = (
    "meta_app_secret",
    "meta_page_access_token",
    "meta_verify_token",
    "openrouter_api_key",
    "twilio_auth_token",
)

# Plain settings editable in the portal.
PLAIN_FIELDS = (
    "bot_mode",
    "public_base_url",
    "meta_page_id",
    "meta_app_id",
    "meta_graph_api_version",
    "ai_model",
    "ai_fallback_model",
    "twilio_account_sid",
    "twilio_phone_number",
    "owner_phone_numbers",
    "tax_rate_bps",
    "pending_order_expiry_minutes",
    "large_order_threshold_cents",
    "customer_can_cancel_accepted",
    "pause_bot_on_staff_reply",
)


def _adapter(field: str) -> TypeAdapter:
    info = Settings.model_fields[field]
    ann = info.annotation
    return TypeAdapter(Annotated[ann, *info.metadata] if info.metadata else ann)


def validate_value(field: str, value: Any) -> Any:
    if field not in PLAIN_FIELDS:
        raise KeyError(field)
    if field == "owner_phone_numbers" and isinstance(value, str):
        value = [p.strip() for p in value.replace("\n", ",").split(",") if p.strip()]
    return _adapter(field).validate_python(value)


def set_values(conn: sqlite3.Connection, values: dict[str, Any], actor: str) -> dict[str, Any]:
    """Validate all values first, then save them together."""
    clean = {k: validate_value(k, v) for k, v in values.items()}
    for k, v in clean.items():
        conn.execute(
            """INSERT INTO app_settings (key, value, updated_by) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by,
                 updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
            (k, json.dumps(v), actor),
        )
    return clean


def effective_settings(conn: sqlite3.Connection, base: Settings, store: SecretStore | None) -> Settings:
    updates: dict[str, Any] = {}
    for row in conn.execute("SELECT key, value FROM app_settings"):
        if row["key"] in PLAIN_FIELDS:
            updates[row["key"]] = json.loads(row["value"])
    if store is not None:
        for name in SECRET_FIELDS:
            value = store.get(conn, name)
            if value is not None:
                updates[name] = SecretStr(value)
    return base.model_copy(update=updates) if updates else base


def set_marker(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO runtime_markers (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (key, value),
    )


def get_marker(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT value, updated_at FROM runtime_markers WHERE key = ?", (key,)).fetchone()
