"""Photos customers send on Messenger.

The owner chose the simple version: there's no image AI. A photo sent while ordering a
Special Design Donut is saved, kept with the customer's cart and then their order, and
shown to the owners (sent with the Messenger alert, and in the portal). A photo sent once a
design order is already in goes with that order, and the owners get it right away. Any other
photo (a complaint, a question) goes to a person.

Photos are untrusted: they are only fetched from Meta's own CDN hosts, size-capped, and
kept only if the bytes really are a JPEG, PNG, GIF or WebP image. Nothing reads what's in them.
"""

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import Settings
from app.services import cart as cart_svc
from app.timeutil import to_db

# Meta serves Messenger attachments from these domains.
ALLOWED_HOST_SUFFIXES = (".fbcdn.net", ".fbsbx.com", ".facebook.com")
MAX_REDIRECTS = 3
MAX_PER_MESSAGE = 5
# The customer is asking for a design donut ("a custom birthday donut", "the design I want").
_DESIGN_TALK = re.compile(r"\b(?:design\w*|custom\w*|decorat\w*|personali[sz]ed|birthday\s+donut|special\s+donut)\b", re.I)
# Sprinkle asked about the design ("What design would you like?", "send a photo of the design").
_DESIGN_ASK = re.compile(r"\bdesign\b[^.!?\n]{0,60}\?|\b(?:photo|picture|pic|image)\b[^.!?\n]{0,40}\bdesign\b"
                         r"|\bdesign\b[^.!?\n]{0,40}\b(?:photo|picture|pic|image)\b", re.I)
OPEN_STATUSES = ("PENDING_ACCEPTANCE", "ACCEPTED", "PREPARING", "READY")


class PhotoError(Exception):
    pass


@dataclass(frozen=True)
class Photo:
    id: int
    path: str | None
    error: str | None


def uploads_dir(settings: Settings) -> Path:
    return Path(settings.uploads_path) if settings.uploads_path else Path(settings.database_path).parent / "uploads"


def file_for(settings: Settings, rel: str) -> Path | None:
    """The file for a stored relative path, only if it is really inside the uploads folder."""
    base = uploads_dir(settings).resolve()
    f = (base / rel).resolve()
    return f if f.is_relative_to(base) and f.is_file() else None


def sniff(data: bytes) -> tuple[str, str] | None:
    """(mime, extension) from the file's first bytes, never from what the sender claims."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def _host_ok(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host.endswith(s) for s in ALLOWED_HOST_SUFFIXES)


async def fetch(url: str, max_bytes: int, http: httpx.AsyncClient | None = None) -> bytes:
    """Download a Messenger attachment: Meta's hosts only (checked again on every redirect),
    and at most `max_bytes`."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=20)
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not _host_ok(url):
                raise PhotoError("the photo link isn't from Messenger")
            async with http.stream("GET", url, follow_redirects=False) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code != 200:
                    raise PhotoError(f"download failed (HTTP {resp.status_code})")
                if int(resp.headers.get("content-length") or 0) > max_bytes:
                    raise PhotoError("the photo is too large")
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > max_bytes:
                        raise PhotoError("the photo is too large")
                return bytes(data)
        raise PhotoError("too many redirects")
    except httpx.HTTPError as exc:
        raise PhotoError(f"download failed ({type(exc).__name__})") from exc
    finally:
        if own:
            await http.aclose()


def existing(conn: sqlite3.Connection, mid: str, seq: int) -> Photo | None:
    row = conn.execute("SELECT id, path, error FROM customer_photos WHERE message_external_id = ? AND seq = ?",
                       (mid, seq)).fetchone()
    return Photo(row["id"], row["path"], row["error"]) if row else None


def save(conn: sqlite3.Connection, settings: Settings, customer_id: int, mid: str, seq: int, data: bytes) -> Photo:
    """Store one photo's bytes. Safe to repeat for the same message (a retry)."""
    if (done := existing(conn, mid, seq)) is not None:
        return done
    kind = sniff(data)
    if kind is None:
        return record_failure(conn, customer_id, mid, seq, "not a JPEG, PNG, GIF or WebP image")
    mime, ext = kind
    digest = hashlib.sha256(data).hexdigest()
    rel = f"{customer_id}/{digest[:24]}.{ext}"
    target = uploads_dir(settings) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        tmp = target.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(target)
    cur = conn.execute(
        """INSERT INTO customer_photos (customer_id, message_external_id, seq, path, mime, bytes, sha256)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (customer_id, mid, seq, rel, mime, len(data), digest),
    )
    return Photo(cur.lastrowid, rel, None)


def record_failure(conn: sqlite3.Connection, customer_id: int, mid: str, seq: int, error: str) -> Photo:
    if (done := existing(conn, mid, seq)) is not None:
        return done
    cur = conn.execute(
        "INSERT INTO customer_photos (customer_id, message_external_id, seq, error) VALUES (?, ?, ?, ?)",
        (customer_id, mid, seq, error[:200]),
    )
    return Photo(cur.lastrowid, None, error)


def design_in_cart(conn: sqlite3.Connection, customer_id: int) -> bool:
    return conn.execute(
        """SELECT 1 FROM carts c JOIN cart_items i ON i.cart_id = c.id JOIN menu_items m ON m.id = i.item_id
           WHERE c.customer_id = ? AND c.status = 'active' AND m.custom_rules IS NOT NULL LIMIT 1""",
        (customer_id,),
    ).fetchone() is not None


def open_design_order(conn: sqlite3.Connection, customer_id: int, now) -> sqlite3.Row | None:
    """The customer's latest design-donut order that's in and not picked up yet."""
    marks = ",".join("?" * len(OPEN_STATUSES))
    return conn.execute(
        f"""SELECT o.* FROM orders o
            WHERE o.customer_id = ? AND o.status IN ({marks}) AND o.pickup_time > ?
              AND EXISTS (SELECT 1 FROM order_items oi JOIN menu_items m ON m.id = oi.item_id
                          WHERE oi.order_id = o.id AND m.custom_rules IS NOT NULL)
            ORDER BY o.id DESC LIMIT 1""",
        (customer_id, *OPEN_STATUSES, to_db(now)),
    ).fetchone()


def asking_for_design(conn: sqlite3.Connection, customer_id: int, session_start_id: int = 0) -> bool:
    """Did the customer just ask for a design donut? Their latest message (not a photo note)
    talks about one, or Sprinkle's reply after it asked about the design. Sprinkle's menu list
    ("Special Design Donut $22.89") or an older mention doesn't count, so a complaint photo
    isn't taken for a design."""
    rows = conn.execute(
        """SELECT m.role, m.content FROM messages m JOIN conversations v ON v.id = m.conversation_id
           WHERE v.customer_id = ? AND m.id >= ? AND m.role IN ('customer', 'assistant')
           ORDER BY m.id DESC LIMIT 12""",
        (customer_id, session_start_id),
    ).fetchall()
    for r in rows:
        text = r["content"] or ""
        if r["role"] == "assistant":
            if _DESIGN_ASK.search(text) and "$" not in text:
                return True
            continue
        if text.startswith("[") and not text.startswith("[button]"):
            continue  # a photo or system note, not the customer's words
        return bool(_DESIGN_TALK.search(text))
    return False


def design_context(conn: sqlite3.Connection, customer_id: int, session_start_id: int = 0) -> bool:
    """Is this customer ordering a Special Design Donut right now? One is in their cart, or
    they just asked for one."""
    return design_in_cart(conn, customer_id) or asking_for_design(conn, customer_id, session_start_id)


def attach_to_cart(conn: sqlite3.Connection, customer_id: int, photo_ids: list[int]) -> int:
    """Keep these photos with the customer's current cart (made if needed)."""
    if not photo_ids:
        return 0
    cart = cart_svc.get_or_create_cart(conn, customer_id)
    marks = ",".join("?" * len(photo_ids))
    return conn.execute(
        f"UPDATE customer_photos SET cart_id = ? WHERE customer_id = ? AND order_id IS NULL AND id IN ({marks})",
        (cart["id"], customer_id, *photo_ids),
    ).rowcount


def attach_to_order(conn: sqlite3.Connection, customer_id: int, order_id: int, photo_ids: list[int]) -> int:
    """Keep photos sent after the order went in with that order."""
    if not photo_ids:
        return 0
    marks = ",".join("?" * len(photo_ids))
    return conn.execute(
        f"UPDATE customer_photos SET order_id = ? WHERE customer_id = ? AND order_id IS NULL AND id IN ({marks})",
        (order_id, customer_id, *photo_ids),
    ).rowcount


def move_to_order(conn: sqlite3.Connection, cart_id: int, order_id: int) -> int:
    return conn.execute("UPDATE customer_photos SET order_id = ? WHERE cart_id = ? AND order_id IS NULL",
                        (order_id, cart_id)).rowcount


def for_order(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM customer_photos WHERE order_id = ? AND path IS NOT NULL ORDER BY id",
                        (order_id,)).fetchall()


def for_customer(conn: sqlite3.Connection, customer_id: int, limit: int = 12) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM customer_photos WHERE customer_id = ? AND path IS NOT NULL ORDER BY id DESC LIMIT ?",
                        (customer_id, limit)).fetchall()


def history_note(photo_ids: list[int], order_id: int | None = None, dropped: int = 0) -> str:
    """What the assistant sees in place of the photo: it can't see images, so it must not guess.
    Notes in square brackets only ever come from the system: customers' own brackets are
    replaced (see conversation.customer_text)."""
    ids = ", ".join(f"#{i}" for i in photo_ids)
    what = "a photo" if len(photo_ids) == 1 else f"{len(photo_ids)} photos"
    where = (f"It's saved with their order #{order_id}, and the owners have it now." if order_id else
             "It's saved with their order for our team to see.")
    extra = (f" They sent {dropped} more that couldn't be kept (only {MAX_PER_MESSAGE} per message); "
             "they've been told to send those in a new message if they matter." if dropped else "")
    return (f"[The customer sent {what} ({ids}). {where}{extra} "
            "You can't see photos, so don't describe or guess what's in it.]")
