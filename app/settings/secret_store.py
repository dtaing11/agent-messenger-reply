"""Encrypted credential storage (AES-256-GCM).

The master key lives in a file outside the database (MASTER_KEY_PATH, created with 0600
permissions on first use). Each value is encrypted with a fresh 96-bit nonce, and the secret
name is bound as associated data so a ciphertext can't be swapped between fields.
Values are write-only from the portal's point of view: callers get "configured" status,
never the plaintext, except where a route explicitly needs it (e.g. revealing the webhook
verify token, which must be pasted into Meta).
"""

import hashlib
import os
import secrets
import sqlite3
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.database.connection import transaction


def load_or_create_key(path: Path) -> bytes:
    path = Path(path)
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise RuntimeError(f"master key at {path} must be exactly 32 bytes")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    key = AESGCM.generate_key(bit_length=256)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class SecretStore:
    def __init__(self, key: bytes):
        self._aead = AESGCM(key)
        self.key_id = hashlib.sha256(key).hexdigest()[:12]

    @classmethod
    def from_path(cls, path: Path) -> "SecretStore":
        return cls(load_or_create_key(path))

    def put(self, conn: sqlite3.Connection, name: str, value: str, actor: str) -> None:
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, value.encode(), name.encode())
        with transaction(conn):
            conn.execute("UPDATE secret_versions SET active = 0 WHERE name = ? AND active = 1", (name,))
            conn.execute(
                "INSERT INTO secret_versions (name, ciphertext, nonce, key_id, created_by) VALUES (?, ?, ?, ?, ?)",
                (name, ct, nonce, self.key_id, actor),
            )

    def get(self, conn: sqlite3.Connection, name: str) -> str | None:
        row = conn.execute(
            "SELECT ciphertext, nonce, key_id FROM secret_versions WHERE name = ? AND active = 1", (name,)
        ).fetchone()
        if row is None:
            return None
        if row["key_id"] != self.key_id:
            raise RuntimeError(f"secret {name!r} was encrypted with a different master key")
        return self._aead.decrypt(row["nonce"], row["ciphertext"], name.encode()).decode()

    def remove(self, conn: sqlite3.Connection, name: str) -> None:
        conn.execute("UPDATE secret_versions SET active = 0 WHERE name = ? AND active = 1", (name,))

    @staticmethod
    def status(conn: sqlite3.Connection, name: str) -> dict:
        row = conn.execute(
            "SELECT created_at FROM secret_versions WHERE name = ? AND active = 1", (name,)
        ).fetchone()
        return {"configured": row is not None, "updated_at": row["created_at"] if row else None}
