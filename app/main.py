"""FastAPI entry point. Only /webhooks/* should be exposed through the Cloudflare Tunnel."""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.config import get_settings
from app.database.connection import connect, migrate
from app.deps import get_db
from app.services.menu import seed_menu
from app.webhooks import messenger
from app.workers import queue

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.db_path = settings.database_path
    conn = connect(settings.database_path)
    applied = migrate(conn)
    if applied:
        log.info("applied migrations: %s", applied)
    seed_menu(conn, settings.menu_path)
    conn.close()
    yield


def create_app() -> FastAPI:
    # Interactive docs are disabled: they must never be reachable from the public hostname.
    app = FastAPI(title="Town Donut Messenger Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(messenger.router)

    @app.get("/health")
    def health(conn=Depends(get_db)):
        conn.execute("SELECT 1")
        return {"status": "ok", "queue": queue.stats(conn)}

    return app


app = create_app()
