# Sprinkle eval set

Synthetic customer conversations (no real customer data) with expected outcomes, and a runner
that scores Sprinkle against the real model. Re-run it after every prompt, menu or code change.

## Run it

```bash
# On the Mac, from the repo folder, with the venv active.
# A separate OpenRouter key from the live bot's is best, so eval spend is easy to see.
export OPENROUTER_API_KEY=sk-or-...
python -m evals.run                          # all cases, the live bot's models by default
python -m evals.run --category discounts     # one category
python -m evals.run --case biscuit_or_burrito --repeat 5   # one case, 5 times (models vary run to run)
python -m evals.run --semantic               # with meaning-based search on (downloads ~70 MB once)
python -m evals.run --check                  # only check the cases still fit the menu (no model calls)
```

It prints the pass rate per category and every failed check. The full transcripts go to
`evals/results/<time>.json` (not committed). Nothing is sent to Messenger or Twilio: each case
runs through the real message worker with fake Messenger and SMS and a throwaway database
seeded from `data/menu.json` and `data/business.json`. Expect about 1 to 3 model calls per turn.

`tests/test_evals.py` runs in the normal test suite. It checks every case against the menu
(item ids, add-ons, choices, expected prices) and runs the harness end to end with a scripted
fake model.

## Categories

| File | What it covers |
|---|---|
| `normal_orders` | Full orders, several items, biscuit and burrito lines, add-ons, changing quantities, removing items, cancelling |
| `clarify` | When to ask instead of guessing: biscuit or burrito (and the tapped answer), flavors, Filled Donut choices, "tomorrow at 7", after-hours times |
| `menu_rules` | No sprinkles on Glazed or Cinnamon Sugar, Big Donuts = $2.09 donuts only, quantity limits, items not sold |
| `drinks` | Sweetness required for milk tea and green tea, no default toppings, toppings offered once, no toppings on Coffee or Cappuccino, house blend vs Community Coffee |
| `design_donuts` | Message and design questions, $22.89, ordered the day before, name and phone by 3 AM, long-name surcharge |
| `time_rules` | 4:30 AM to 12:15 PM pickups, lead time, booking horizon, weekday names, holidays, the one contact reminder |
| `discounts` | Percent off, price match, coupons, BOGO, birthday, employee price, notes smuggling, jailbreaks |
| `security` | Prompt injection, system prompt leaks, owner phone privacy, other customers' data, the credit line, no "we'll call you" |
| `store_info` | Hours, location, payment, tax, freshness, meaning-based search, misspellings, location on the summary |
| `photos` | Design reference photos kept with the order; other photos go to staff |
| `robustness` | Rambling messages, angry customers, asking for a person, Modify, other languages |

Every case also checks the store-wide rules in `ALWAYS_NEVER` (`evals/runner.py`): no internal
button notes, no "we'll call you", no granted discounts, no tax math.

## Writing a case

```json
{"id": "long_john_flavor_then_tap",
 "description": "Ask the flavor, take the tapped answer.",
 "now": "2026-11-18 08:00",
 "turns": [{"say": "2 long johns"}, {"tap": "Maple"}],
 "expect": {"cart": [{"item_id": "long_john", "quantity": 2, "details": {"flavor": "Maple"}}], "subtotal_cents": 418}}
```

- `now` is store time (America/Chicago). Wednesday 2026-11-18 is the usual day.
- Turns:
  - `say`: the customer types
  - `tap`: tap a button Sprinkle offered (matched by text; typed instead if there's no such button)
  - `confirm` / `modify`: tap a button on the last summary
  - `photo`: the customer sends an image
  - `wait_minutes`: time passes, and housekeeping runs (reminders, expiry)
- Expectations:
  - Cart: `cart` (with `cart_exact`, default true), `cart_empty`, `not_in_cart`, `cart_line_never`, `subtotal_cents`
  - Pickup and order: `pickup` (`null` = not set), `pickup_name`, `summary_shown`, `order_submitted`, `order_status`
  - Handoff and photos: `handoff`, `photos_attached`
  - Replies: `reply_any`, `last_reply_any`, `never`, `max_replies_matching`, `buttons_any` (regexes, case-insensitive)
  - Tools: `tools_called`, `tools_not_called`
- Write regexes loosely. Check what must be true, not the exact wording.
