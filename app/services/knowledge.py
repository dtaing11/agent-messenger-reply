"""Approved business knowledge (FAQ) with SQLite FTS5 keyword search.

Only owner-approved documents are searchable (the FTS table is maintained by triggers on
approval_status). app.services.search adds meaning-based matches (FastEmbed + Qdrant) on top;
prices, availability and order state always come from SQL tools, never from here.
"""

import json
import re
import sqlite3
from pathlib import Path

from app.database.connection import transaction


def seed_knowledge(conn: sqlite3.Connection, path: Path, location: str) -> int:
    """Load owner-confirmed FAQ docs as approved knowledge (version bumps when text changes)."""
    path = Path(path)
    if not path.exists():
        return 0
    docs = json.loads(path.read_text())["documents"]
    with transaction(conn):
        for d in docs:
            cur = conn.execute(
                "SELECT version, question, answer FROM knowledge_documents WHERE id = ? AND approval_status = 'approved'",
                (d["id"],),
            ).fetchone()
            if cur and cur["question"] == d["question"] and cur["answer"] == d["answer"]:
                continue
            version = 1
            if cur:
                conn.execute(
                    "UPDATE knowledge_documents SET approval_status = 'retired' WHERE id = ? AND approval_status = 'approved'",
                    (d["id"],),
                )
                version = conn.execute("SELECT MAX(version) FROM knowledge_documents WHERE id = ?", (d["id"],)).fetchone()[0] + 1
            conn.execute(
                """INSERT INTO knowledge_documents (id, version, question, answer, category, location, source_type, approval_status)
                   VALUES (?, ?, ?, ?, ?, ?, 'owner', 'approved')""",
                (d["id"], version, d["question"], d["answer"], d.get("category", "general"), location),
            )
    return len(docs)


STOPWORDS = frozenset(
    "the and are you your yours can could would will what when where which who how does did have has with for from "
    "that this there here they them our ours any some get got just like want need please thanks thank hey hello "
    "about into than then also too very much many tell know".split()
)


def search(conn: sqlite3.Connection, query: str, limit: int = 4) -> list[dict]:
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2 and w not in STOPWORDS]
    if not words:
        return []
    # OR of prefix terms; bm25 ranks the best matches first.
    fts_query = " OR ".join(f'"{w}"*' for w in words[:12])
    rows = conn.execute(
        """SELECT doc_id, version, question, answer, bm25(knowledge_fts) AS score FROM knowledge_fts
           WHERE knowledge_fts MATCH ? ORDER BY score LIMIT ?""",
        (fts_query, limit),
    ).fetchall()
    return [{"id": r["doc_id"], "question": r["question"], "answer": r["answer"]} for r in rows]
