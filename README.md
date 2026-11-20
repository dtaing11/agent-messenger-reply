# Town Donut & Breakfast: Messenger ordering agent

Self-hosted FastAPI service that answers the Town Donut & Breakfast Facebook Page on
Messenger, builds pickup orders, and texts the owners for acceptance. Full scope lives in
the project plan (v3). This repository currently covers **Phase 1–2**: the Messenger
webhook, durable queue, SQLite schema, cart, deterministic totals and the order state machine.

> `data/menu.json` is the real menu (transcribed from the owner's menu board, 2026-11-19).
> Store hours are 4:30 AM to 12:30 PM every day, holidays included. **Pickup lead time and booking horizon are still placeholders.**

## What works today

| Area | Status |
|---|---|
| `GET /webhooks/messenger` verification (`hub.verify_token`) | done |
| `POST /webhooks/messenger`: `X-Hub-Signature-256` check over the raw body, constant-time compare | done |
| Dedup by page + message id, persisted before the 200 is returned | done |
| Durable SQLite queue: per-customer ordering, cross-customer concurrency, retry with backoff, stale-lock recovery | done |
| Echo handling: our own echoes ignored; staff replies from the Page inbox pause the bot for that customer | done |
| Attachments hand off to a human | done |
| SQLite schema + forward-only migrations, WAL, foreign keys, FTS5 table for Phase 3 | done |
| Menu seeding from `data/menu.json` (removed items become unavailable, never deleted) | done |
| Cart with modifiers, quantity limits, versioning | done |
| Integer-cent totals, configurable tax (half-up rounding) | done |
| Pickup validation: hours, closed dates, lead time, booking horizon, slot size, DST | done |
| Confirmation tokens bound to customer + cart version + expiry | done |
| Atomic order + owner-SMS outbox rows (one per owner phone), idempotent re-confirmation | done |
| Order transitions with conditional updates and an audit trail; pending-order expiry; customer cancel rules | done |
| Messenger Send API client (quick-reply Confirm / Modify) | written, mocked only (no Meta app yet) |
| LLM agent (OpenRouter) | **Phase 5**: a fixed placeholder reply is sent for now |
| RAG (FastEmbed + Qdrant + FTS5 retrieval) | Phase 3 |
| Twilio sending + owner SMS commands | Phase 6 (outbox rows are already written) |
| Developer portal | done (first version, see below) |

## Developer portal

A private web page for setting everything up without editing files:
**http://127.0.0.1:8001/admin/** (localhost only, never exposed through the tunnel).

- **First run:** the portal prints a one-time setup code (also saved to `data/bootstrap_code.txt`,
  deleted after use). Open `/admin/setup`, enter it, and create your admin account.
- **Integrations:** paste your Meta, OpenRouter and Twilio details. Credentials are encrypted
  (AES-GCM, key in `MASTER_KEY_PATH`) and never shown again. **Detect tunnel** fills in the
  public URL from a running `cloudflared` quick tunnel. **Connect automatically** checks the
  tunnel, sets the Meta app's webhook to your callback URL and subscribes your Page.
- **Overview:** bot on/off, worker heartbeat, queue, last webhook, integration status
  (refreshes every 10 s). The bot starts **paused** and won't reply until you turn it on.
- **Business:** owner phones, tax, order expiry, large-order flag, staff-reply pause.
- Saved values apply on the next message, with no restart. Values saved in the portal
  override `.env`.

Security: Argon2id passwords, server-side sessions, CSRF on every change, login lockout,
strict CSP (no inline scripts, no CDNs; HTMX and Pico CSS are vendored), audit log without
secret values. Not yet built from the plan: multiple users/roles, versioned config rollback,
backups page, log viewer.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # set META_VERIFY_TOKEN, META_APP_SECRET, etc.
pytest -q
python -m app.run_dev                     # webhook app :8000 + portal :8001 + worker
cloudflared tunnel --url http://localhost:8000   # public URL for Meta (in another terminal)
```

## Run on the M1 Mac with Docker

All images (`python:3.12-slim`, `cloudflare/cloudflared`, `qdrant/qdrant`) are multi-arch, so
Docker Desktop on Apple Silicon builds and runs them natively as `linux/arm64`.

```bash
cp .env.example .env
docker compose up -d --build                  # api + worker
docker compose --profile tunnel up -d         # add Cloudflare Tunnel (set CLOUDFLARE_TUNNEL_TOKEN)
```

The API is bound to `127.0.0.1:8000`. In the Cloudflare Tunnel config, route only
`/webhooks/*` to it; `/health` and future `/admin/*` routes must stay private. Interactive
API docs are disabled.

## Assumptions (open questions)

Each is configurable in `.env` (or `data/*.json`) so the answer doesn't need a code change.

| Question | Assumed for now | Setting |
|---|---|---|
| Real menu and prices | **Confirmed:** from the menu board, 40 items; extras (meat $0.99, egg/cheese $0.50) are modifiers; biscuit sandwiches have a free "Make it a burrito" option | `data/menu.json` |
| Store hours, holidays, lead time | **Confirmed:** 4:30 AM–12:30 PM every day, no holiday closures. Assumed: 30 min lead; 14 days ahead; 15 min slots | `data/business.json` |
| Charge tax in the summary? | **Confirmed: no.** Summaries and owner SMS say prices are before tax and remind the customer tax is added at pickup | `TAX_RATE_BPS=0` |
| Pending order expiry | Unanswered orders expire after 120 min | `PENDING_ORDER_EXPIRY_MINUTES` (`0` = never) |
| Customer cancellation | Allowed while pending; after acceptance only the owner can cancel | `CUSTOMER_CAN_CANCEL_ACCEPTED` |
| Owner phones | **Confirmed: two.** Every number gets the order SMS and may send commands | `OWNER_PHONE_NUMBERS` (comma-separated) |
| Staff reply by hand? | Yes, and a manual reply pauses the bot for that customer | `PAUSE_BOT_ON_STAFF_REPLY` |
| Large orders | Subtotal of $100 or more is flagged in the SMS | `LARGE_ORDER_THRESHOLD_CENTS` |
| Special Design Donut | **Confirmed:** $22.89, always asks for the message and the design, owner always reviews. Long-name surcharge (names over 7 letters) amount unknown, so the store confirms it when accepting | `custom` block on `special_design_donut` in `data/menu.json` (`long_name_surcharge_cents`) |
| Languages | **Confirmed: English only** (keeps `bge-small-en-v1.5`) | `EMBEDDING_MODEL` |
| Historical inbox import | Off | `HISTORICAL_IMPORT_ENABLED` |
| Meta developer app | **Confirmed: not created yet**; webhook is tested with signed fixtures | `META_*` |

## Layout

```text
app/
  main.py                  public webhook app (+ /health), port 8000
  admin/                   developer portal app (auth, pages), port 8001
  settings/                runtime settings + encrypted secret store
  integrations/checks.py   Meta/OpenRouter/Twilio tests, automatic Meta connection
  run_dev.py               runs both apps + worker in one process
  config.py                env-based settings
  agent/state_machine.py   order + conversation state machines
  webhooks/messenger.py    verify, signature, dedup, enqueue
  workers/queue.py         durable SQLite queue
  workers/message_worker.py  event processing (python -m app.workers.message_worker)
  integrations/messenger.py  Send API client
  services/                menu, cart, pricing, business hours, orders, conversations
  database/                connection, migration runner, migrations/*.sql
developer_portal/          Jinja2 templates + vendored HTMX/Pico CSS
data/                      real menu.json, business.json (hours still placeholder; DB files are git-ignored)
tests/                     pytest suite (uses tests/fixtures/menu.json, not the real menu)
```
