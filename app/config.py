"""Bootstrap configuration.

Values come from environment variables (or a local .env). Business rules the owner has not
confirmed yet are deliberately configurable so they can change without code edits; the
developer portal (Phase 9) will later layer versioned runtime settings on top of this.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Meta Messenger
    meta_graph_api_version: str = "v21.0"
    meta_page_id: str = ""
    meta_page_access_token: SecretStr = SecretStr("")
    meta_app_secret: SecretStr = SecretStr("")
    meta_verify_token: SecretStr = SecretStr("")
    # Our app's id: echoes carrying it are our own replies; other echoes are staff typing in
    # the Page inbox, which pauses the bot for that customer (if enabled below).
    meta_app_id: str = ""
    pause_bot_on_staff_reply: bool = True
    # Sent while the LLM agent (Phase 5) is not wired up yet.
    placeholder_reply: str = (
        "Thanks for messaging Town Donut & Breakfast! I'm an automated assistant that's still being set up. "
        "A team member will get back to you soon."
    )

    # OpenRouter (Phase 5)
    openrouter_api_key: SecretStr = SecretStr("")
    ai_model: str = ""
    ai_fallback_model: str = ""
    max_tool_rounds: int = Field(6, ge=1, le=20)
    ai_temperature: float = Field(0.2, ge=0, le=1.5)
    ai_timeout_seconds: float = Field(25.0, ge=5, le=120)
    # Room for models that think before answering; too low and replies come back empty.
    ai_max_tokens: int = Field(1500, ge=100, le=8000)
    # Ask OpenRouter to route only to providers that don't train on prompts.
    ai_deny_data_collection: bool = True
    ai_history_messages: int = Field(16, ge=2, le=60)
    # After this many quiet hours a returning customer gets a fresh start (0 = never).
    session_idle_hours: float = Field(12, ge=0, le=24 * 30)

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = ""
    # Owner confirmed two phones: every listed number gets order SMS and may send commands.
    owner_phone_numbers: Annotated[list[str], NoDecode] = Field(default_factory=list)
    sms_status_callback_url: str = ""

    # Store / business rules (placeholders until the owner confirms)
    store_timezone: str = "America/Chicago"
    store_location: str = "watson"
    tax_rate_bps: int = Field(0, ge=0, le=5000)
    pending_order_expiry_minutes: int = Field(120, ge=0)
    customer_can_cancel_accepted: bool = False
    require_customer_phone: bool = True
    pickup_check_after_minutes: int = Field(30, ge=0, le=720)  # 0 = don't ask owners
    # Where owner alerts go. "messenger": owners' linked Facebook accounts, with a text to the owner
    # phones as the backup whenever no linked owner can be reached on Messenger (Meta's 24-hour rule).
    owner_alert_channel: Literal["messenger", "sms", "both"] = "messenger"
    # Store-local hour to text the owners the day's orders that were placed on an earlier day (-1 = off).
    owner_reminder_hour: int = Field(3, ge=-1, le=23)
    large_order_threshold_cents: int = Field(10000, ge=0)
    confirmation_token_ttl_minutes: int = Field(30, ge=1)

    # Storage
    database_path: Path = Path("./data/database.db")
    menu_path: Path = Path("./data/menu.json")
    business_path: Path = Path("./data/business.json")
    knowledge_path: Path = Path("./data/knowledge/faq.json")
    qdrant_url: str = "http://qdrant:6333"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    rag_top_k: int = 5

    # Runtime / portal
    # "paused": messages are recorded but the bot never replies. Start paused until setup is done.
    bot_mode: Literal["paused", "live"] = "paused"
    public_base_url: str = ""  # e.g. https://orders.example.com (the tunnel hostname)
    master_key_path: Path = Path("./data/keys/master.key")  # encryption key for stored credentials
    admin_session_hours: int = Field(12, ge=1, le=168)
    admin_host: str = "127.0.0.1"
    admin_port: int = 8001
    cloudflared_metrics_ports: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [20241, 20242, 20243, 20244, 20245])

    # Worker
    worker_poll_seconds: float = 0.5
    worker_max_attempts: int = Field(8, ge=1)
    worker_lock_timeout_seconds: int = 300
    worker_concurrency: int = Field(4, ge=1, le=32)
    historical_import_enabled: bool = False

    @field_validator("owner_phone_numbers", "cloudflared_metrics_ports", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
