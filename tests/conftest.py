from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.config import Settings
from app.database.connection import connect, migrate
from app.services.business import load_business
from app.services.menu import seed_menu

ROOT = Path(__file__).resolve().parent.parent

# Wednesday 2026-11-18, 8:00 AM Central (store open 5:30-13:00 in the placeholder hours).
NOW = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp_path / "test.db",
        menu_path=ROOT / "data/menu.json",
        business_path=ROOT / "data/business.json",
        meta_app_secret="test-secret",
        meta_verify_token="verify-me",
        meta_page_id="PAGE1",
        meta_app_id="APP1",
        owner_phone_numbers=["+15550000001", "+15550000002"],
        tax_rate_bps=0,
    )


@pytest.fixture
def conn(settings):
    c = connect(settings.database_path)
    migrate(c)
    seed_menu(c, settings.menu_path)
    yield c
    c.close()


@pytest.fixture
def biz():
    return load_business(ROOT / "data/business.json")


@pytest.fixture
def customer_id(conn):
    from app.services.conversation import get_or_create_customer

    return get_or_create_customer(conn, "PAGE1", "PSID1")["id"]
