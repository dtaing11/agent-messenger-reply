"""FastAPI dependencies shared by the public and admin apps."""

import sqlite3
from collections.abc import Iterator

from fastapi import Depends, Request

from app.config import Settings, get_settings
from app.database.connection import connect
from app.settings.runtime import effective_settings


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """One short-lived connection per request (SQLite connections are cheap; WAL allows
    concurrent readers alongside the worker and the other listener)."""
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_runtime_settings(
    request: Request, conn: sqlite3.Connection = Depends(get_db), base: Settings = Depends(get_settings)
) -> Settings:
    """Settings snapshot for this request: env defaults + values saved in the portal."""
    return effective_settings(conn, base, getattr(request.app.state, "secret_store", None))
