# Town Donut & Breakfast: Messenger ordering agent

Self-hosted FastAPI service that answers the Town Donut & Breakfast Facebook Page on
Messenger, builds pickup orders, and texts the owners for acceptance. Full scope lives in
the project plan (v3). It covers the Messenger webhook and durable queue, the order backend,
the developer portal, the AI ordering assistant ("Sprinkle", DeepSeek via OpenRouter with
tools), store FAQ search, owner texts through Twilio (with text-back commands), customer
order updates on Messenger, a menu editor, and customer profiles with pickup history.

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
| Photos: a photo sent while ordering a Special Design Donut is saved with the order (owners see it on the Messenger alert and in the portal; Sprinkle never describes it); any other photo or file hands the chat to a human. Photos are only fetched from Facebook's own servers, up to `PHOTO_MAX_MB`, and kept in `UPLOADS_PATH` | done |
| SQLite schema + forward-only migrations, WAL, foreign keys, FTS5 table for Phase 3 | done |
| Menu seeding from `data/menu.json` (removed items become unavailable, never deleted) | done |
| Cart with modifiers, quantity limits, versioning | done |
| Integer-cent totals, configurable tax (half-up rounding) | done |
| Pickup validation: hours, closed dates, lead time, booking horizon, slot size, DST; the last pickup is 12:15 PM (15 minutes before closing), and Sprinkle says so whenever it gives the hours | done |
| Confirmation tokens bound to customer + cart version + expiry | done |
| Atomic order + owner-SMS outbox rows (one per owner phone), idempotent re-confirmation | done |
| Order transitions with conditional updates and an audit trail; pending-order expiry; customer cancel rules | done |
| Messenger Send API client (quick-reply Confirm / Modify; long messages split, never cut) | done |
| AI assistant: OpenRouter tool loop (menu, FAQ, cart, pickup, summary, cancel, hand-off), fallback model, privacy routing | done |
| Store FAQ (owner-approved docs in `data/knowledge/faq.json`) | done |
| Hybrid search for the menu and FAQ: keywords (names, descriptions, search words, FTS5) plus meaning (FastEmbed `bge-small-en-v1.5` in an embedded Qdrant index), merged with reciprocal rank fusion, so "something chocolatey", "the long one with cream" or "fruity tea" find the right items. Prices, availability and approval always come from SQL. The index updates itself after menu edits in the portal; until the model has downloaded, search is keyword-only. Check results with `python -m app.search_check "your words"` | done |
| Menu descriptions (what the bot can tell customers) and search words (nicknames and misspellings, never shown), both editable in the portal | done |
| Owner texts via Twilio with retries; delivery status callback | done |
| Owner text commands: `YES 12`, `NO 12`, `READY 12`, `PICKED 12`, `NOSHOW 12`, `ORDERS`, `PAUSE`, `RESUME`, `HELP` | done |
| Customer Messenger updates on accept / decline / ready / expiry (within Meta's 24 h window) | done |
| Pickup check: owners are asked "picked up?" after each pickup time; no-shows kept per Messenger account (owner-only, never matched by phone number) and flagged on their next order | done |
| Customer phone number collected with name and pickup time | done |
| One reminder when a customer picked items but never gave the pickup name or phone number: about an hour after their last message (15 minutes if the pickup time is close), once per cart, never while staff have the chat, the bot is paused, the cart was set aside, after the pickup time or outside Meta's 24-hour window (`CONTACT_REMINDER_MINUTES`, `CONTACT_REMINDER_SOON_MINUTES`) | done |
| Special Design Donut must be ordered by the day before pickup (never same day; not a full 24 hours) and confirmed, with the pickup name and phone number, by 3 AM on the pickup day, when the team starts baking. Checked on every pickup time, summary and when the order is sent; the reminder names the deadline (`notice_cutoff_hour` in `data/business.json`) | done |
| 3 AM text to the owners on the pickup day listing orders placed on an earlier day (`OWNER_REMINDER_HOUR`) | done |
| Fresh start after 12 quiet hours: the AI sees only the new messages; an unfinished cart is set aside and offered back once (`SESSION_IDLE_HOURS`) | done |
| Short menu answers on phones: per-item prices from `browse_menu`, one level at a time, tap-to-pick quick replies | done |
| Owner alerts on Facebook Messenger: owners link their account with a one-time 8-character code (portal → Owner alerts) and someone approves it there; then they tap Accept/Decline, Picked up/No-show, or reply with an exact command like `YES 12`; texts are the backup | done |
| Linking safety: 5 wrong codes lock that Messenger account out of linking for an hour; 20 wrong codes overall only warn the owners (a valid code always works, and nothing links until someone approves it in the portal). The portal's Owner alerts page shows the wrong tries and can clear them | done |
| Owner commands are exact: the order number is always required, and anything else ("ok see you at 5") changes nothing | done |
| No discounts through the bot: no tool takes a price; order notes, design details, the pickup name and tap buttons can't mention one; Sprinkle's replies are checked for discount promises ("10% off", "on the house", "knock a dollar off" and so on, while honest refusals pass) and fake "confirmed" claims (one rewrite, then a safe reply) | done |
| Milk Tea and Green Tea ask how sweet (Less sweet, Regular, More sweet); no drink comes with a topping; toppings only go on Green Tea, Milk Tea, Iced Coffee and Smoothie (other drinks have no add-ons, so the cart refuses them), and Sprinkle offers them once, leading with tapioca pearls. The Iced Coffee is the house blend; only Coffee and Cappuccino are Community Coffee (and replies saying otherwise are rewritten) | done |
| Free-text mixes are checked: counts must add up to the box, sprinkles only on icing flavors, Big Donuts only from the $2.09 donuts; "any" or "random" is accepted only on its own (store's choice), never to slip past these checks | done |
| Filled Donut (Glazed, Chocolate Icing or Powdered Sugar outside; Bavarian Cream, Blueberry, Raspberry, Lemon or Strawberry jelly filling) is its own item, separate from the Long John ($2.09, a Big Donut) | done |
| Store FAQ includes freshness: the staff start baking at 3 AM every day and nothing is left over | done |
| Each reply's cart changes are all or nothing: an error part way through is undone, so a retry never adds items twice | done |
| Meta's 24-hour rule for owner alerts: daily 👍 check-in sent inside the window; undeliverable alerts are held and sent when the owner next writes, and texted if no owner can be reached | done |
| Menu editor in the portal (prices, items, add-ons, categories, flavors) | done |
| Developer portal | done (see below) |

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
- **Menu:** change prices, rename, hide/show, add and remove items, edit add-ons and flavor
  choices, add/rename/reorder categories. Changes apply to the bot immediately. Once you edit
  here, startup stops re-importing `data/menu.json` (a button re-imports it on purpose, and
  **Download menu.json** backs up the current menu).
- **Orders:** accept, decline, ready, picked up, no-show (same as the owner text commands).
- **Customers:** name, phone, orders, picked-up and no-show counts, recent messages, and a
  **Resume the bot** button after a hand-off.
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
| Real menu and prices | **Confirmed:** from the menu board; extras (meat $0.99, egg/cheese $0.50) are modifiers; biscuits and burritos are separate items at the same price; Twist, Long John and Icing donuts need a flavor; Filled Donut needs an outside and a filling; sprinkles only on icing flavors; "Big Donuts" = every single donut priced $2.09 | `data/menu.json` or the portal Menu page |
| Pickup location | **Confirmed:** Town Donut Watson, Oak Point Plaza next to Subway, 35505 LA-16 #3C, Denham Springs, LA 70706 (shown on every summary and confirmation) | `data/business.json` |
| Customer phone required | Yes | `REQUIRE_CUSTOMER_PHONE` |
| When to ask owners "picked up?" | 30 min after pickup time | `PICKUP_CHECK_AFTER_MINUTES` (`0` = never) |
| Store hours, holidays, lead time | **Confirmed:** 4:30 AM–12:30 PM every day, no holiday closures. Assumed: 30 min lead; 14 days ahead; 15 min slots | `data/business.json` |
| Charge tax in the summary? | **Confirmed: no.** Summaries and owner SMS say prices are before tax and remind the customer tax is added at pickup | `TAX_RATE_BPS=0` |
| Pending order expiry | Unanswered orders expire 120 min after the store is open (an evening order waits for the morning), and never later than the pickup time. Owners can't accept an order whose pickup time has passed. | `PENDING_ORDER_EXPIRY_MINUTES` (`0` = only at the pickup time) |
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
