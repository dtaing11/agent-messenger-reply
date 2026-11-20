"""Run everything for local development in one process:

  * public webhook app   http://127.0.0.1:8000   (point the Cloudflare Tunnel here)
  * developer portal     http://127.0.0.1:8001/admin/   (localhost only, never tunnel this)
  * message worker

    python -m app.run_dev
"""

import asyncio
import logging

import uvicorn

from app.config import get_settings
from app.database.bootstrap import prepare
from app.workers.message_worker import run_worker


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    prepare(settings)

    public = uvicorn.Server(uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, proxy_headers=True, log_level="info"))
    admin = uvicorn.Server(uvicorn.Config("app.admin.app:app", host=settings.admin_host, port=settings.admin_port, log_level="info"))
    await asyncio.gather(public.serve(), admin.serve(), run_worker(settings))


if __name__ == "__main__":
    asyncio.run(main())
