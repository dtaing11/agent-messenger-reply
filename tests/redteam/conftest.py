import pytest
from tests.conftest import *  # noqa: F401,F403  (conn, biz, customer_id, NOW)
from tests.conftest import ROOT
from app.config import Settings
from app.services.business import load_business


@pytest.fixture
def settings(tmp_path) -> Settings:
    # Real production data files, not the test fixtures.
    return Settings(
        _env_file=None, database_path=tmp_path / "rt.db",
        menu_path=ROOT / "data/menu.json", business_path=ROOT / "data/business.json",
        knowledge_path=ROOT / "data/knowledge/faq.json",
        meta_app_secret="test-secret", meta_verify_token="verify-me", meta_page_id="PAGE1", meta_app_id="APP1",
        owner_phone_numbers=["+15550000001", "+15550000002"], tax_rate_bps=0, bot_mode="live",
        master_key_path=tmp_path / "keys/master.key",
    )


@pytest.fixture
def biz():
    return load_business(ROOT / "data/business.json")
