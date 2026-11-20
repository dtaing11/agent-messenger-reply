"""Owner alerts on Facebook Messenger.

An owner links their own Facebook account by sending the Page "LINK 123456", a one-time code
made in the portal. After that, their messages to the Page are owner commands (never shown to
the AI), and order alerts can go to them on Messenger.

Meta only lets a Page message a person within 24 hours of that person's last message, and the
old message tags that allowed updates later were retired in 2026 (none ever covered staff
alerts). So: every owner message keeps their window open for 24 hours, a daily check-in with a
tap-to-keep button is sent before it closes (inside the window, so it's ordinary messaging),
and alerts that can't be delivered wait until the owner writes again (plus a text, if Twilio
is set up and nobody's window is open).
"""

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import datetime, time, timedelta

from app.timeutil import from_db, local, to_db

WINDOW = timedelta(hours=24)
CODE_TTL = timedelta(minutes=15)
LINK_RE = re.compile(r"^\s*link\s*#?\s*(\d{6})\s*[.!]?\s*$", re.IGNORECASE)

KEEPALIVE_TEXT = ("Daily check-in: tap 👍 so I can keep sending you order alerts here. "
                  "Facebook only lets the Page message you within 24 hours of your last message.")
KEEPALIVE_BUTTONS = [["👍 Keep alerts on", "KEEPALIVE"], ["Open orders", "ORDERS"]]
# The check-in goes out during the day (store-local) once the window has CHECKIN_LEFT left, or in
# the evening when the window would otherwise close overnight or early the next morning.
CHECKIN_FROM, CHECKIN_UNTIL, EVENING = time(8), time(21), time(20)
CHECKIN_LEFT = timedelta(hours=3)


class LinkError(ValueError):
    pass


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_link_code(conn: sqlite3.Connection, label: str, created_by: str, now: datetime) -> str:
    label = " ".join((label or "").split())[:40]
    if not label:
        raise LinkError("Give the owner a name, like \"Dina\".")
    code = f"{secrets.randbelow(1_000_000):06d}"
    conn.execute("INSERT INTO owner_link_codes (code_hash, label, created_by, expires_at) VALUES (?, ?, ?, ?)",
                 (_hash(code), label, created_by, to_db(now + CODE_TTL)))
    return code


def link_code_in(text: str | None) -> str | None:
    m = LINK_RE.match(text or "")
    return m.group(1) if m else None


def link(conn: sqlite3.Connection, page_id: str, psid: str, code: str, now: datetime) -> sqlite3.Row:
    """Use a code: this Messenger account becomes an owner account. Raises LinkError."""
    row = conn.execute("SELECT * FROM owner_link_codes WHERE code_hash = ?", (_hash(code),)).fetchone()
    if row is None or row["used_at"] or from_db(row["expires_at"]) <= now:
        raise LinkError("That code isn't valid anymore. Make a new one in the portal (Owner alerts).")
    if not conn.execute("UPDATE owner_link_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
                        (to_db(now), row["code_hash"])).rowcount:
        raise LinkError("That code was already used.")
    conn.execute(
        """INSERT INTO owner_accounts (page_id, psid, label, last_message_at) VALUES (?, ?, ?, ?)
           ON CONFLICT(page_id, psid) DO UPDATE SET label = excluded.label, active = 1,
             last_message_at = excluded.last_message_at, keepalive_sent_at = NULL""",
        (page_id, psid, row["label"], to_db(now)),
    )
    return find(conn, page_id, psid)


def find(conn: sqlite3.Connection, page_id: str, psid: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM owner_accounts WHERE page_id = ? AND psid = ? AND active = 1",
                        (page_id, psid)).fetchone()


def active(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM owner_accounts WHERE active = 1 ORDER BY id").fetchall()


def all_accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM owner_accounts ORDER BY active DESC, id").fetchall()


def remove(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("UPDATE owner_accounts SET active = 0 WHERE id = ?", (account_id,))


def window_closes_at(account: sqlite3.Row) -> datetime | None:
    return from_db(account["last_message_at"]) + WINDOW if account["last_message_at"] else None


def window_open(account: sqlite3.Row, now: datetime) -> bool:
    closes = window_closes_at(account)
    return bool(closes and now < closes - timedelta(minutes=1))


def window_open_for(conn: sqlite3.Connection, psid: str, now: datetime) -> bool:
    row = conn.execute("SELECT * FROM owner_accounts WHERE psid = ? AND active = 1", (psid,)).fetchone()
    return bool(row and window_open(row, now))


def touch(conn: sqlite3.Connection, account_id: int, now: datetime) -> int:
    """The owner wrote to the Page: their 24-hour window restarts. Alerts that were held because
    the window had closed are queued again (from the last day only). Returns how many."""
    acc = conn.execute("SELECT * FROM owner_accounts WHERE id = ?", (account_id,)).fetchone()
    conn.execute("UPDATE owner_accounts SET last_message_at = ?, keepalive_sent_at = NULL WHERE id = ?",
                 (to_db(now), account_id))
    return conn.execute(
        """UPDATE notifications SET status = 'pending', attempts = 0, next_attempt_at = ?, last_error = NULL
           WHERE channel = 'messenger' AND recipient = ? AND status = 'failed' AND last_error LIKE 'window_closed%'
             AND created_at >= ?""",
        (to_db(now), acc["psid"], to_db(now - WINDOW)),
    ).rowcount


def due_for_checkin(account: sqlite3.Row, now: datetime, tz: str) -> bool:
    if not window_open(account, now):
        return False  # too late: a message now would break Meta's rule
    if account["keepalive_sent_at"] and account["keepalive_sent_at"] >= account["last_message_at"]:
        return False  # one check-in per window
    now_local = local(now, tz)
    if not (CHECKIN_FROM <= now_local.time() < CHECKIN_UNTIL):
        return False  # don't buzz owners at night
    closes = window_closes_at(account)
    if closes - now <= CHECKIN_LEFT:
        return True
    next_morning = datetime.combine(now_local.date() + timedelta(days=1), CHECKIN_FROM, tzinfo=now_local.tzinfo)
    return now_local.time() >= EVENING and closes < next_morning + CHECKIN_LEFT


def queue_checkins(conn: sqlite3.Connection, tz: str, now: datetime) -> list[int]:
    """Send the daily keep-alerts-on check-in to owners whose window is about to close."""
    sent = []
    for acc in active(conn):
        if not due_for_checkin(acc, now, tz):
            continue
        if conn.execute("UPDATE owner_accounts SET keepalive_sent_at = ? WHERE id = ? AND "
                        "(keepalive_sent_at IS NULL OR keepalive_sent_at < last_message_at)", (to_db(now), acc["id"])).rowcount:
            conn.execute(
                """INSERT OR IGNORE INTO notifications (type, recipient, body, idempotency_key, channel, buttons)
                   VALUES ('owner_checkin', ?, ?, ?, 'messenger', ?)""",
                (acc["psid"], KEEPALIVE_TEXT, f"checkin:{acc['id']}:{acc['last_message_at']}", json.dumps(KEEPALIVE_BUTTONS)),
            )
            sent.append(acc["id"])
    return sent
