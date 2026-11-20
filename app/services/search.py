"""Hybrid search: keyword matches and meaning matches, merged with reciprocal rank fusion.

Keywords find exact names ("long john", "kolache"); meaning finds the rest ("something
chocolatey", "the long one with cream", "fruity tea"). Results are always read back from SQL,
so prices, availability and FAQ approval come from the database, never from the index.
"""

import logging
import sqlite3

from app.services import knowledge, menu, semantic

log = logging.getLogger(__name__)

RRF_K = 60
# A meaning match must clear the minimum score and sit near the best one, so an unrelated
# question ("do you sell pizza") doesn't pull in random items.
NEAR_BEST = 0.10


def rrf(*rankings: list[str], k: int = RRF_K) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda d: -scores[d])  # stable: ties keep keyword order


# --- documents --------------------------------------------------------------------------

def menu_documents(conn: sqlite3.Connection) -> dict[str, str]:
    """{item_id: text} for every item a customer can order right now."""
    from app.services.custom_items import parse_choices

    docs = {}
    for r in conn.execute(f"SELECT * FROM ({menu._ITEMS})").fetchall():
        options = [o for c in parse_choices(r["choices"]) if not c.is_pool for o in c.options]
        parts = [f"{r['name']}.", f"{r['category_name']}.", (r["description"] or "").strip()]
        if options:
            parts.append("Choices: " + ", ".join(options) + ".")
        if (terms := (r["search_terms"] or "").strip()):
            parts.append(f"Also called: {terms}.")
        docs[r["id"]] = " ".join(p for p in parts if p)
    return docs


def faq_documents(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["id"]: f"{r['question']} {r['answer']}" for r in conn.execute(
        "SELECT id, question, answer FROM knowledge_documents WHERE approval_status = 'approved'")}


def sync_all(conn: sqlite3.Connection, index: semantic.SemanticIndex) -> None:
    index.sync("menu", menu_documents(conn))
    index.sync("faq", faq_documents(conn))


def _meaning(conn: sqlite3.Connection, kind: str, query: str, limit: int, min_score: float) -> list[str]:
    index = semantic.get_index()
    if index is None or not query.strip():
        return []
    try:
        index.sync(kind, menu_documents(conn) if kind == "menu" else faq_documents(conn))
        hits = index.search(kind, query, limit)
    except Exception:  # noqa: BLE001 - never let the index break a reply
        log.exception("semantic %s search failed; using keywords only", kind)
        return []
    if not hits:
        return []
    best = hits[0][1]
    return [d for d, score in hits if score >= min_score and score >= best - NEAR_BEST]


# --- menu -------------------------------------------------------------------------------

def _keyword_menu(conn: sqlite3.Connection, query: str, limit: int) -> list[sqlite3.Row]:
    rows = menu.search_menu(conn, query, limit=limit)
    if not rows:  # try each word on its own before giving up
        seen: dict[str, sqlite3.Row] = {}
        for w in query.split():
            for r in menu.search_menu(conn, w, limit=5):
                seen.setdefault(r["id"], r)
        rows = list(seen.values())[:limit]
    return rows


def find_menu_items(conn: sqlite3.Connection, query: str, limit: int = 8,
                    min_score: float = 0.62) -> tuple[list[sqlite3.Row], bool]:
    """(rows, exact). exact is False when only meaning matches were found, so the caller can
    say these are the closest items rather than a match."""
    if not menu.query_terms(query):
        return menu.search_menu(conn, query, limit=limit), True
    keyword = _keyword_menu(conn, query, 20)
    meaning = _meaning(conn, "menu", query, 20, min_score)
    order = rrf([r["id"] for r in keyword], meaning)[:limit]
    if not order:
        return [], True
    marks = ",".join("?" * len(order))
    found = {r["id"]: r for r in conn.execute(f"SELECT * FROM ({menu._ITEMS}) WHERE id IN ({marks})", order)}
    return [found[i] for i in order if i in found], bool(keyword)


# --- FAQ --------------------------------------------------------------------------------

def find_knowledge(conn: sqlite3.Connection, query: str, limit: int = 4, min_score: float = 0.62) -> list[dict]:
    keyword = knowledge.search(conn, query, limit=8)
    meaning = _meaning(conn, "faq", query, 8, min_score)
    order = rrf([d["id"] for d in keyword], meaning)[:limit]
    if not order:
        return []
    marks = ",".join("?" * len(order))
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, question, answer FROM knowledge_documents WHERE approval_status = 'approved' AND id IN ({marks})", order)}
    return [{"id": i, "question": rows[i]["question"], "answer": rows[i]["answer"]} for i in order if i in rows]


def warm(settings) -> None:
    """Start loading the model in the background and index everything once it's ready."""
    from app.database.connection import connect

    def first_sync(index: semantic.SemanticIndex) -> None:
        conn = connect(settings.database_path)
        try:
            sync_all(conn, index)
        finally:
            conn.close()

    semantic.start(settings, on_ready=first_sync)

