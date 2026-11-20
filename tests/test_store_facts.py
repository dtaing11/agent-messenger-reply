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


def test_phone_number_is_asked_for_the_pickup_person(biz):
    """Owner, 2026-11-20: staff only contact a customer who doesn't show up, so Sprinkle must not
    say the store will reach out about the order."""
    from app.agent.prompts import system_prompt
    from app.timeutil import local
    from tests.conftest import NOW

    prompt = system_prompt(biz, local(NOW, biz.timezone))
    assert "Can I have the phone number for the person who will pick up the order, please?" in prompt
    assert "Never say the store will call, text or reach out about the order" in prompt
    assert "so the store can reach them" not in prompt
