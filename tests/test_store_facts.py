"""Owner-confirmed store facts the assistant answers from (data/knowledge/faq.json)."""

import pytest

from app.services.knowledge import search, seed_knowledge
from tests.conftest import ROOT


@pytest.mark.parametrize("question", ["Are the donuts fresh?", "are these from yesterday", "leftover donuts?",
                                      "do you bake every day"])
def test_freshness_answer(conn, question):
    seed_knowledge(conn, ROOT / "data/knowledge/faq.json", "watson")
    docs = search(conn, question)
    assert docs and docs[0]["id"] == "freshness"
    assert "3 AM" in docs[0]["answer"] and "Nothing is left over" in docs[0]["answer"]
