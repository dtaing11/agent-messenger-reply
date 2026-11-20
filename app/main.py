"""Public listener (port 8000): Messenger and Twilio webhooks and a minimal health check only.

This is the only app the Cloudflare Tunnel should point at. The developer portal is a
separate app (app.admin.app) on a localhost-only listener.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from app.config import get_settings
from app.database.bootstrap import prepare
from app.deps import get_db
from app.settings.secret_store import SecretStore
from app.webhooks import messenger, sms

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.db_path = settings.database_path
    app.state.secret_store = SecretStore.from_path(settings.master_key_path)
    applied = prepare(settings)
    if applied:
        log.info("applied migrations: %s", applied)
    yield


def create_app() -> FastAPI:
    # Interactive docs are disabled: they must never be reachable from the public hostname.
    app = FastAPI(title="Town Donut Messenger Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(messenger.router)
    app.include_router(sms.router)

    @app.get("/health")
    def health(conn=Depends(get_db)):
        conn.execute("SELECT 1")
        return {"status": "ok"}  # public: nothing about the queue or orders (that's in the portal)

    return app


app = create_app()
