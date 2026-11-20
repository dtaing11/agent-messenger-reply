"""Portal authentication: Argon2id passwords, server-side sessions, CSRF, bootstrap code."""

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.settings.runtime import get_marker, set_marker
from app.timeutil import from_db, to_db

COOKIE = "td_admin"
_ph = PasswordHasher()  # argon2id with library defaults
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
BOOTSTRAP_TTL_HOURS = 24


@dataclass(frozen=True)
class AdminUser:
    id: int
    username: str
    role: str
    csrf_token: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def audit(conn: sqlite3.Connection, actor: str, action: str, target: str | None, outcome: str) -> None:
    """Record an admin action. Never pass secret values here."""
    conn.execute(
        "INSERT INTO audit_events (actor, action, target, outcome) VALUES (?, ?, ?, ?)", (actor, action, target, outcome)
    )


def has_admin(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone() is not None


def issue_bootstrap_code(conn: sqlite3.Connection, now: datetime, code_file: Path | None = None) -> str | None:
    """Create a one-time code for creating the first admin. Returns it (to print locally),
    or None if an admin already exists."""
    if has_admin(conn):
        return None
    code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
    set_marker(conn, "admin.bootstrap", f"{_hash_token(code)}|{to_db(now + timedelta(hours=BOOTSTRAP_TTL_HOURS))}")
    if code_file is not None:
        code_file.parent.mkdir(parents=True, exist_ok=True)
        code_file.unlink(missing_ok=True)
        import os

        fd = os.open(code_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(code + "\n")
    return code


def create_first_admin(conn: sqlite3.Connection, code: str, username: str, password: str, now: datetime,
                       code_file: Path | None = None) -> None:
    if has_admin(conn):
        raise PermissionError("An admin already exists.")
    marker = get_marker(conn, "admin.bootstrap")
    if marker is None:
        raise PermissionError("No setup code is active. Restart the portal to print a new one.")
    code_hash, expires = marker["value"].split("|")
    if from_db(expires) <= now or not secrets.compare_digest(code_hash, _hash_token(code.strip().upper())):
        raise PermissionError("That setup code is wrong or expired.")
    username = username.strip()
    if not (3 <= len(username) <= 40) or not username.replace("_", "").replace(".", "").isalnum():
        raise ValueError("Username must be 3-40 letters, numbers, dots or underscores.")
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    conn.execute(
        "INSERT INTO admin_users (username, password_hash, role) VALUES (?, ?, 'developer_admin')",
        (username, _ph.hash(password)),
    )
    conn.execute("DELETE FROM runtime_markers WHERE key = 'admin.bootstrap'")  # one-time
    if code_file is not None:
        code_file.unlink(missing_ok=True)
    audit(conn, username, "admin.create_first", username, "ok")


def _recent_failures(conn: sqlite3.Connection, username: str, now: datetime) -> int:
    since = to_db(now - timedelta(minutes=LOCKOUT_MINUTES))
    return conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE action = 'login' AND outcome = 'failed' AND target = ? COLLATE NOCASE AND created_at >= ?",
        (username, since),
    ).fetchone()[0]


def login(conn: sqlite3.Connection, username: str, password: str, now: datetime, session_hours: int) -> str:
    """Return a new session token, or raise PermissionError."""
    username = username.strip()
    if _recent_failures(conn, username, now) >= MAX_FAILED_LOGINS:
        audit(conn, username, "login", username, "locked")
        raise PermissionError(f"Too many failed attempts. Try again in {LOCKOUT_MINUTES} minutes.")
    row = conn.execute("SELECT * FROM admin_users WHERE username = ? AND disabled = 0", (username,)).fetchone()
    try:
        if row is None:
            _ph.hash(password)  # similar timing for unknown users
            raise VerifyMismatchError
        _ph.verify(row["password_hash"], password)
    except VerifyMismatchError:
        conn.execute(
            "INSERT INTO audit_events (actor, action, target, outcome, created_at) VALUES (?, 'login', ?, 'failed', ?)",
            (username, username, to_db(now)),
        )
        raise PermissionError("Wrong username or password.")
    if _ph.check_needs_rehash(row["password_hash"]):
        conn.execute("UPDATE admin_users SET password_hash = ? WHERE id = ?", (_ph.hash(password), row["id"]))
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO admin_sessions (token_hash, user_id, csrf_token, expires_at) VALUES (?, ?, ?, ?)",
        (_hash_token(token), row["id"], secrets.token_urlsafe(24), to_db(now + timedelta(hours=session_hours))),
    )
    conn.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (to_db(now),))
    audit(conn, row["username"], "login", row["username"], "ok")
    return token


def session_user(conn: sqlite3.Connection, token: str | None, now: datetime) -> AdminUser | None:
    if not token:
        return None
    row = conn.execute(
        """SELECT u.id, u.username, u.role, s.csrf_token, s.expires_at FROM admin_sessions s
           JOIN admin_users u ON u.id = s.user_id WHERE s.token_hash = ? AND u.disabled = 0""",
        (_hash_token(token),),
    ).fetchone()
    if row is None or from_db(row["expires_at"]) <= now:
        return None
    return AdminUser(row["id"], row["username"], row["role"], row["csrf_token"])


def logout(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (_hash_token(token),))
