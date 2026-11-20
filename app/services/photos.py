"""Photos customers send on Messenger.

The owner chose the simple version: there's no image AI. A photo sent while ordering a
Special Design Donut is saved, kept with the customer's cart and then their order, and
shown to the owners (sent with the Messenger alert, and in the portal). A photo with no
design-donut context goes to a person.

Photos are untrusted: they are only fetched from Meta's own CDN hosts, size-capped, and
kept only if the bytes really are a JPEG, PNG, GIF or WebP image. Nothing reads what's in them.
"""

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import Settings
from app.services import cart as cart_svc

# Meta serves Messenger attachments from these domains.
ALLOWED_HOST_SUFFIXES = (".fbcdn.net", ".fbsbx.com", ".facebook.com")
MAX_REDIRECTS = 3


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


def design_context(conn: sqlite3.Connection, customer_id: int, session_start_id: int = 0) -> bool:
    """Is this customer ordering a Special Design Donut right now? Either one is in their cart,
    or the last few messages of this session talk about a design."""
    in_cart = conn.execute(
        """SELECT 1 FROM carts c JOIN cart_items i ON i.cart_id = c.id JOIN menu_items m ON m.id = i.item_id
           WHERE c.customer_id = ? AND c.status = 'active' AND m.custom_rules IS NOT NULL LIMIT 1""",
        (customer_id,),
    ).fetchone()
    if in_cart:
        return True
    rows = conn.execute(
        """SELECT m.content FROM messages m JOIN conversations v ON v.id = m.conversation_id
           WHERE v.customer_id = ? AND m.id >= ? AND m.role IN ('customer', 'assistant')
           ORDER BY m.id DESC LIMIT 6""",
        (customer_id, session_start_id),
    ).fetchall()
    return any("design" in (r["content"] or "").lower() for r in rows)


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


def move_to_order(conn: sqlite3.Connection, cart_id: int, order_id: int) -> int:
    return conn.execute("UPDATE customer_photos SET order_id = ? WHERE cart_id = ? AND order_id IS NULL",
                        (order_id, cart_id)).rowcount


def for_order(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM customer_photos WHERE order_id = ? AND path IS NOT NULL ORDER BY id",
                        (order_id,)).fetchall()


def for_customer(conn: sqlite3.Connection, customer_id: int, limit: int = 12) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM customer_photos WHERE customer_id = ? AND path IS NOT NULL ORDER BY id DESC LIMIT ?",
                        (customer_id, limit)).fetchall()


def history_note(photo_ids: list[int]) -> str:
    """What the assistant sees in place of the photo: it can't see images, so it must not guess."""
    ids = ", ".join(f"#{i}" for i in photo_ids)
    what = "a photo" if len(photo_ids) == 1 else f"{len(photo_ids)} photos"
    return (f"[The customer sent {what} ({ids}). It's saved with their order for our team to see. "
            "You can't see photos, so don't describe or guess what's in it.]")
