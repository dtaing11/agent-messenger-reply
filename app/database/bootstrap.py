"""Prepare the database at startup: migrations, then menu and store FAQ from data/.

The menu file is imported only until the menu is edited in the portal (see menu_admin)."""

import logging
import sqlite3

from app.config import Settings
from app.database.connection import connect, migrate
from app.services.business import load_business
from app.services.knowledge import seed_knowledge
from app.services.menu import seed_menu
from app.services.menu_admin import edited_in_portal, ensure_categories

log = logging.getLogger(__name__)


def prepare(settings: Settings, conn: sqlite3.Connection | None = None) -> list[str]:
    own = conn is None
    conn = conn or connect(settings.database_path)
    try:
        applied = migrate(conn)
        if edited_in_portal(conn):
            ensure_categories(conn)
            log.info("menu was edited in the portal; not re-importing %s", settings.menu_path)
        else:
            seed_menu(conn, settings.menu_path)
        seed_knowledge(conn, settings.knowledge_path, load_business(settings.business_path).location)
        return applied
    finally:
        if own:
            conn.close()
