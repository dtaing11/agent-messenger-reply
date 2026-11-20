"""Hybrid menu and FAQ search: keywords plus meaning, merged with reciprocal rank fusion.

A tiny fake embedder stands in for FastEmbed (it maps a few words to shared "meanings"), and
an in-memory Qdrant stands in for the index on disk, so these tests run offline.
"""

import asyncio
import math
import re
import zlib
from pathlib import Path

import pytest

from app.agent import tools
from app.database.connection import connect, migrate
from app.services import knowledge, menu, menu_admin, search, semantic
from tests.conftest import NOW

ROOT = Path(__file__).resolve().parent.parent
SAME = {  # word -> the meaning it shares with others
    "chocolatey": "chocolate", "choc": "chocolate", "cocoa": "chocolate",
    "fruity": "fruit", "mango": "fruit", "strawberry": "fruit", "peach": "fruit", "passion": "fruit",
    "pineapple": "fruit", "raspberry": "fruit", "apple": "fruit",
    "boba": "bubble", "pearl": "bubble", "tapioca": "bubble",
    "creamy": "cream", "custard": "cream", "bavarian": "cream",
    "baked": "fresh", "bake": "fresh", "fresh": "fresh", "leftover": "fresh", "yesterday": "fresh",
    "open": "hours", "close": "hours", "hours": "hours", "time": "hours",
}
DIM = 2048


class FakeEmbedder:
    dim = DIM

    def __init__(self):
        self.embedded: list[str] = []

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for w in re.findall(r"[a-z]+", text.lower()):
            if len(w) < 3:
                continue
            w = SAME.get(w, w[:-1] if w.endswith("s") else w)
            v[zlib.crc32(w.encode()) % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def passages(self, texts):
        self.embedded.extend(texts)
        return [self._vec(t) for t in texts]

    def query(self, text):
        return self._vec(text)


@pytest.fixture
def real_menu(settings):
    """The real menu (with descriptions and search words), not the small test fixture."""
    c = connect(settings.database_path)
    migrate(c)
    menu.seed_menu(c, ROOT / "data/menu.json")
    knowledge.seed_knowledge(c, ROOT / "data/knowledge/faq.json", "watson")
    yield c
    c.close()


@pytest.fixture
def index():
    from qdrant_client import QdrantClient

    idx = semantic.SemanticIndex(FakeEmbedder(), QdrantClient(":memory:"))
    semantic.set_index(idx)
    yield idx
    semantic.set_index(None)


def names(rows):
    return [r["name"] for r in rows]


def test_rrf_rewards_items_both_searches_agree_on():
    assert set(search.rrf(["a", "b", "c"], ["c", "b", "x"])[:2]) == {"b", "c"}
    assert search.rrf(["a"], []) == ["a"]
    assert search.rrf([], ["x", "y"]) == ["x", "y"]


def test_without_an_index_search_is_keyword_only(real_menu):
    assert semantic.get_index() is None
    rows, exact = search.find_menu_items(real_menu, "long john")
    assert names(rows)[0] == "Long John" and exact
    rows, exact = search.find_menu_items(real_menu, "something chocolatey")
    assert any("Chocolate" in n for n in names(rows))  # found through the search words
    assert search.find_menu_items(real_menu, "pizza") == ([], True)


def test_search_words_and_plurals_help_keyword_search(real_menu):
    assert names(search.find_menu_items(real_menu, "longjohn")[0])[0] == "Long John"
    assert "Filled Donut" in names(search.find_menu_items(real_menu, "jelly donut")[0])
    assert "Milk Tea" in names(search.find_menu_items(real_menu, "boba")[0])
    assert "Boudin Cheese Kolache" in names(search.find_menu_items(real_menu, "boudain")[0])
    assert "Apple Fritter" in names(search.find_menu_items(real_menu, "fritters")[0])


def test_meaning_finds_what_keywords_miss(real_menu, index):
    real_menu.execute("UPDATE menu_items SET search_terms = ''")  # keywords alone can't find these now
    rows, exact = search.find_menu_items(real_menu, "cocoa", min_score=0.3)
    assert not exact and rows and all("hocolate" in (r["name"] + r["description"] + (r["choices"] or "")) for r in rows)
    rows, _ = search.find_menu_items(real_menu, "fruity tea", min_score=0.3)
    assert {"Green Tea", "Milk Tea"} & set(names(rows[:3]))


def test_unrelated_questions_find_nothing(real_menu, index):
    assert search.find_menu_items(real_menu, "pizza", min_score=0.3) == ([], True)


def test_prices_and_availability_always_come_from_sql(real_menu, index):
    search.sync_all(real_menu, index)
    index.sync = lambda *a, **k: 0  # pretend the index is stale
    real_menu.execute("UPDATE menu_items SET price_cents = 999 WHERE id = 'long_john'")
    real_menu.execute("UPDATE menu_items SET available = 0 WHERE id = 'twist'")
    rows, _ = search.find_menu_items(real_menu, "long john twist")
    assert "Twist" not in names(rows)
    assert next(r for r in rows if r["id"] == "long_john")["price_cents"] == 999


def test_portal_edit_reindexes_only_that_item(real_menu, index):
    search.sync_all(real_menu, index)
    index.embedder.embedded.clear()
    menu_admin.update_item(real_menu, "pinecone", {"description": "A pinecone-shaped pastry.", "search_terms": "pine cone, cone",
                                                   "available": "on"})
    search.find_menu_items(real_menu, "cone")
    assert len(index.embedder.embedded) == 1 and "pinecone-shaped" in index.embedder.embedded[0]
    # Archiving an item takes it out of the index.
    menu_admin.remove_item(real_menu, "pinecone")
    search.find_menu_items(real_menu, "cone")
    assert "pinecone" not in index._indexed["menu"]


def test_index_on_disk_survives_a_restart_without_reembedding(real_menu, tmp_path):
    from qdrant_client import QdrantClient

    first = semantic.SemanticIndex(FakeEmbedder(), QdrantClient(path=str(tmp_path / "vi")))
    search.sync_all(real_menu, first)
    assert len(first.embedder.embedded) > 40
    first.client.close()
    again = semantic.SemanticIndex(FakeEmbedder(), QdrantClient(path=str(tmp_path / "vi")))
    search.sync_all(real_menu, again)
    assert again.embedder.embedded == []
    assert again.search("menu", "long john", 3)[0][0] == "long_john"
    again.client.close()


def test_faq_search_merges_keywords_and_meaning(real_menu, index):
    docs = search.find_knowledge(real_menu, "is everything baked today or from yesterday", min_score=0.2)
    assert docs and any("3 AM" in d["answer"] or "3 am" in d["answer"].lower() for d in docs)
    # Retired versions never come back, even if the index still had them.
    first = docs[0]["id"]
    real_menu.execute("UPDATE knowledge_documents SET approval_status = 'retired' WHERE id = ?", (first,))
    index.sync = lambda *a, **k: 0
    assert first not in [d["id"] for d in search.find_knowledge(real_menu, "is everything baked today", min_score=0.2)]


def test_tool_says_when_results_are_only_close_matches(real_menu, index, settings, biz):
    from app.services.conversation import get_or_create_customer

    real_menu.execute("UPDATE menu_items SET search_terms = ''")
    cid = get_or_create_customer(real_menu, "PAGE1", "PSID1")["id"]
    s = settings.model_copy(update={"semantic_min_score": 0.3})
    out = asyncio.run(tools.execute(tools.ToolContext(real_menu, cid, s, biz, NOW), "search_menu", {"query": "cocoa"}))
    assert out["items"] and "closest" in out["note"]
    out = asyncio.run(tools.execute(tools.ToolContext(real_menu, cid, s, biz, NOW), "search_menu", {"query": "long john"}))
    assert out["items"][0]["name"] == "Long John" and "note" not in out
    assert "Bavarian cream" in out["items"][0]["description"]


def test_a_broken_model_falls_back_to_keywords(real_menu, settings, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(semantic, "build", boom)
    monkeypatch.setitem(semantic._state, "status", "off")
    t = semantic.start(settings.model_copy(update={"semantic_search": True}))
    t.join(5)
    assert semantic.status()["status"] == "failed" and semantic.get_index() is None
    assert names(search.find_menu_items(real_menu, "long john")[0])[0] == "Long John"
    semantic.set_index(None)


def test_an_unreachable_qdrant_server_uses_the_local_index(settings, tmp_path):
    s = settings.model_copy(update={"qdrant_url": "http://127.0.0.1:9", "vector_index_path": tmp_path / "vi"})
    client = semantic._open_client(s)
    assert (tmp_path / "vi").is_dir()
    client.close()


def test_migration_fills_only_empty_descriptions(settings):
    c = connect(settings.database_path)
    migrate(c)
    c.execute("INSERT INTO menu_items (id, name, category, description, price_cents) VALUES "
              "('glazed_donut', 'Glazed Donut', 'donuts', 'Owner wrote this', 125), "
              "('cake_donut', 'Cake Donut', 'donuts', '', 179)")
    sql = (ROOT / "app/database/migrations/0013_menu_search_words.sql").read_text()
    updates = "\n".join(line for line in sql.splitlines() if line.startswith("UPDATE"))
    c.executescript(updates)
    rows = {r["id"]: r for r in c.execute("SELECT * FROM menu_items")}
    assert rows["glazed_donut"]["description"] == "Owner wrote this"
    assert rows["cake_donut"]["description"].startswith("A cake-style donut")
    assert "cakey" in rows["cake_donut"]["search_terms"]
    c.close()


def test_portal_saves_search_words(real_menu):
    menu_admin.update_item(real_menu, "long_john", {"search_terms": "  longjohn,\n eclair ", "available": "on"})
    assert menu.get_item(real_menu, "long_john")["search_terms"] == "longjohn, eclair"
    item = next(i for i in menu_admin.export_menu(real_menu)["items"] if i["id"] == "long_john")
    assert item["search_terms"] == "longjohn, eclair"
