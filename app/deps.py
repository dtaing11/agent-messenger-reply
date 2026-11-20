"""FastAPI dependencies."""

import sqlite3
from collections.abc import Iterator

from fastapi import Request


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """One short-lived connection per request (SQLite connections are cheap; WAL allows
    concurrent readers with the worker process)."""
    from app.database.connection import connect

    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()
