"""The spending cap and call log, with the fake model (free)."""
import json

import pytest

from vet.llm import LLM, BudgetExceeded, FakeGemini, Ledger

SCHEMA = {"type": "object", "required": ["decision", "reason"],
          "properties": {"decision": {"type": "string", "enum": ["include", "exclude"]},
                         "reason": {"type": "string"}, "extra": {"type": "string"}}}


def llm(tmp_path, cap=5.0, save=True, responder=None):
    return LLM(FakeGemini(responder), cap=cap, save_calls=save, corpus=tmp_path)


def test_fake_answers_fit_the_schema(tmp_path):
    assert llm(tmp_path).call("screen", "title", SCHEMA) == {"decision": "include", "reason": ""}


def test_custom_responder_sees_step_and_prompt(tmp_path):
    seen = []
    m = llm(tmp_path, responder=lambda step, prompt, schema: seen.append((step, prompt)) or
            {"decision": "exclude", "reason": "cats"})
    assert m.call("review", "abc", SCHEMA)["decision"] == "exclude"
    assert seen == [("review", "abc")]


def test_cap_stops_before_going_over(tmp_path):
    m = llm(tmp_path, cap=0.0001)       # one worst-case call costs more than this
    with pytest.raises(BudgetExceeded):
        m.call("structure", "x" * 1000, SCHEMA)
    assert m.backend.calls == []        # refused before any call was made


def test_spending_persists_across_runs(tmp_path):
    m = llm(tmp_path)
    m.call("screen", "x" * 4000, SCHEMA)
    m.call("screen", "x" * 4000, SCHEMA)
    again = Ledger(tmp_path / "spend_fake.csv", cap=5.0)
    assert again.spent == pytest.approx(m.ledger.spent) and again.spent > 0
    assert not (tmp_path / "spend.csv").exists()      # fake calls never count as real spending


def test_calls_are_saved_per_document_and_can_be_switched_off(tmp_path):
    llm(tmp_path).call("structure", "text", SCHEMA, doc="PMID1")
    saved = list((tmp_path / "calls" / "PMID1").glob("*_structure.json"))
    assert len(saved) == 1 and json.loads(saved[0].read_text())["prompt"] == "text"
    llm(tmp_path / "off", save=False).call("structure", "text", SCHEMA, doc="PMID1")
    assert not (tmp_path / "off" / "calls").exists()


def test_unknown_model_is_refused(tmp_path):
    m = llm(tmp_path)
    m.cfg = {**m.cfg, "models": {**m.cfg["models"], "screen": "gemini-unpriced"}}
    with pytest.raises(BudgetExceeded, match="no price"):
        m.call("screen", "x", SCHEMA)
