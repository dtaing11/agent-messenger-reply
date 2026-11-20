"""See what menu and FAQ search finds, with the meaning scores, to tune SEMANTIC_MIN_SCORE.

    python -m app.search_check "something chocolatey" "fruity tea" "are the donuts fresh"

Uses the real model but a throwaway in-memory index, so it works while Sprinkle is running.
Prints item and question names only, never prices or anything private.
"""

import sys

from app.config import get_settings
from app.database.connection import connect
from app.services import search, semantic

DEFAULT_QUERIES = [
    "something chocolatey", "the long one with cream", "fruity tea", "boba", "jelly donut",
    "breakfast sandwich", "something spicy", "birthday donut with a name", "pizza", "cheeseburger",
    "are the donuts fresh", "when do you close", "do you take cards",
]


def main(queries: list[str]) -> None:
    settings = get_settings()
    conn = connect(settings.database_path)
    index = semantic.load_now(settings)
    search.sync_all(conn, index)
    menu_names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM menu_items")}
    faq_q = {r["id"]: r["question"] for r in conn.execute("SELECT id, question FROM knowledge_documents")}
    m = settings.semantic_min_score
    print(f"min score {m} (closest-only menu {m + search.CLOSEST_ONLY_EXTRA:.2f}, faq {m + search.FAQ_OFFSET:.2f}), "
          f"keep within {search.NEAR_BEST} of the best\n")
    for q in queries or DEFAULT_QUERIES:
        print(f"== {q}")
        hits = index.search("menu", q, 6)
        print("   meaning (menu): " + ", ".join(f"{menu_names.get(d, d)} {s:.3f}" for d, s in hits))
        rows, exact = search.find_menu_items(conn, q, min_score=settings.semantic_min_score)
        print(f"   menu result{'' if exact else ' (closest only)'}: " + (", ".join(r["name"] for r in rows) or "nothing"))
        hits = index.search("faq", q, 3)
        print("   meaning (faq):  " + ", ".join(f"{faq_q.get(d, d)[:40]} {s:.3f}" for d, s in hits))
        docs = search.find_knowledge(conn, q, min_score=settings.semantic_min_score)
        print("   faq result: " + (" | ".join(d["question"][:50] for d in docs) or "nothing"))
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
