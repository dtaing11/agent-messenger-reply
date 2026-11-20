"""Meaning-based search with the real model (BAAI/bge-small-en-v1.5) on the real menu and FAQ.

The other search tests use a fake embedder, so they can't tell whether the real model finds
the right things. These need the model, so they run only when it's there:

    python -m app.download_model            # once, ~70 MB into data/models
    SPRINKLE_REAL_MODEL=1 pytest tests/test_search_real_model.py

CI downloads the model and runs them on every push.
"""

import os

import pytest

from app.config import Settings
from app.database.connection import connect, migrate
from app.services import menu, search, semantic
from app.services.business import load_business
from app.services.knowledge import seed_knowledge
from tests.conftest import ROOT

pytestmark = pytest.mark.skipif(not os.environ.get("SPRINKLE_REAL_MODEL"),
                                reason="needs the real model: python -m app.download_model, then SPRINKLE_REAL_MODEL=1")


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("realmodel")
    settings = Settings(_env_file=None, database_path=tmp / "db.sqlite", menu_path=ROOT / "data/menu.json",
                        business_path=ROOT / "data/business.json", knowledge_path=ROOT / "data/knowledge/faq.json",
                        master_key_path=tmp / "keys/master.key", semantic_search=True)
    conn = connect(settings.database_path)
    migrate(conn)
    menu.seed_menu(conn, settings.menu_path)
    seed_knowledge(conn, settings.knowledge_path, load_business(settings.business_path).location)
    index = semantic.load_now(settings)
    search.sync_all(conn, index)
    yield conn, settings, index
    semantic.set_index(None)
    conn.close()


def test_the_model_loads(real):
    _, _, index = real
    assert index.embedder.dim == 384


@pytest.mark.parametrize("query,any_of,top", [
    ("a drink with tapioca balls", {"milk_tea", "green_tea", "smoothie", "iced_coffee"}, 5),
    ("jelly filled donut", {"filled_donut"}, 5),
    ("the long one with cream inside", {"long_john"}, 6),
    ("breakfast sandwich with sausage and egg", {"sausage_egg_cheese_biscuit", "sausage_egg_cheese_burrito",
                                                 "sausage_egg_cheese_croissant"}, 8),
    ("birthday donut with a name on it", {"special_design_donut"}, 5),
    ("something chocolatey", None, 8),
])
def test_meaning_finds_the_right_items(real, query, any_of, top):
    _, _, index = real
    ids = [d for d, _ in index.search("menu", query, top)]
    if any_of is None:  # "something chocolatey": most of the top hits should be chocolate items
        names = [menu.get_item(real[0], i)["name"].lower() + " " + (menu.get_item(real[0], i)["description"] or "").lower()
                 for i in ids]
        assert sum("chocolate" in n for n in names) >= len(names) // 2, ids
    else:
        assert any_of & set(ids), (query, ids)


@pytest.mark.parametrize("query,doc", [
    ("are the donuts from yesterday", "freshness"),
    ("is everything baked fresh every morning", "freshness"),
    ("when do you close", "hours"),
    ("can I pay with a card", "payment"),
    ("do you bring the order to my house", "pickup_only"),
    ("where is the shop", "location"),
])
def test_meaning_finds_the_right_answer(real, query, doc):
    conn, settings, _ = real
    found = [d["id"] for d in search.find_knowledge(conn, query, min_score=settings.semantic_min_score)]
    assert doc in found[:3], (query, found)


@pytest.mark.parametrize("query", ["pizza", "cheeseburger", "ice cream", "sushi"])
def test_things_we_dont_sell_are_never_an_exact_match(real, query):
    conn, settings, _ = real
    rows, exact = search.find_menu_items(conn, query, min_score=settings.semantic_min_score)
    assert not exact, [r["name"] for r in rows]


def test_prices_still_come_from_the_database(real):
    conn, settings, _ = real
    conn.execute("UPDATE menu_items SET price_cents = 777 WHERE id = 'milk_tea'")
    try:
        rows, _ = search.find_menu_items(conn, "a drink with tapioca balls", min_score=settings.semantic_min_score)
        assert any(r["id"] == "milk_tea" and r["price_cents"] == 777 for r in rows)
    finally:
        menu.seed_menu(conn, settings.menu_path)
