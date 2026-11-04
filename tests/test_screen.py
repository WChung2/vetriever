"""Screening: the prompt comes from the topic, and code decides from the model's answers
(fake model, free)."""
import re

import pytest

from vet.codebook import Codebook
from vet.llm import LLM, FakeGemini
from vet.screen import SCHEMA, decide, screen, system_prompt
from vet.settings import ROOT

CB = Codebook()
FELINE = {"question": "Treating feline asthma.",
          "screen": {"species": "cats", "condition": "feline asthma",
                     "also_called": ["allergic bronchitis"], "related": ["chronic bronchitis"],
                     "treatments": ["inhaled fluticasone"]}}


def ans(condition="yes", animals="patients", kind="study", design="retrospective",
        quote="dogs with IMHA"):
    return {"key": "1", "reasoning": "", "kind": kind, "design": design, "summary": "",
            "condition": {"answer": condition, "quote": quote},
            "animals": {"answer": animals, "quote": quote}}


def test_template_names_no_species_or_condition():
    template = (ROOT / "prompts" / "screen.md").read_text("utf-8").lower()
    for topic in CB.topics.values():
        s = topic.get("screen")
        if s:
            words = re.findall(r"[a-z]{4,}", f"{s['species']} {s['condition']}".lower())
            assert not [w for w in words if w in template], words


def test_prompt_is_filled_from_the_topic():
    cats = system_prompt("feline_asthma", FELINE)
    assert "feline asthma" in cats and "patients (cats with" in cats and "dog" not in cats.lower()
    assert "{" not in cats


def test_a_topic_without_its_screen_section_is_refused():
    with pytest.raises(ValueError, match="lacks.*related"):
        system_prompt("x", {"question": "q", "screen": {**FELINE["screen"], "related": []}})
    with pytest.raises(ValueError, match="lacks"):
        system_prompt("y", {"question": "q"})        # no [screen] at all


TEXT = "Retrospective study of 19 cats; dogs with IMHA; in vitro assay of reagents"


@pytest.mark.parametrize("answers, expected", [
    (ans(animals="other_species", quote="19 cats"), ("off_topic", "O1", [])),
    # another species is off topic even when the condition is named
    (ans(condition="yes", animals="other_species", quote="19 cats"), ("off_topic", "O1", [])),
    (ans(condition="no", quote="in vitro assay"), ("off_topic", "O2", [])),
    (ans(animals="none", quote="in vitro assay"), ("off_topic", "O3", [])),
    # healthy animals given a treatment of interest don't need the condition
    (ans(condition="no", animals="healthy_treated"), ("keep", "", ["research_animals"])),
    (ans(condition="related"), ("keep", "", ["related_condition"])),
    (ans(animals="survey"), ("keep", "", ["survey"])),
    (ans(design="single_case"), ("keep", "", ["single_case"])),
    (ans(condition="unclear", animals="unclear"), ("keep", "", [])),
    # a review has no animals of its own, but a review of another condition is off topic
    (ans(kind="review", animals="none"), ("lead", "", [])),
    (ans(kind="review", condition="no", quote="in vitro assay"), ("off_topic", "O2", [])),
])
def test_code_decides_from_the_answers(answers, expected):
    assert decide(answers, TEXT) == expected


@pytest.mark.parametrize("quote", ["", "in 19 horses"])
def test_a_drop_needs_its_quote_in_the_record(quote):
    assert decide(ans(animals="other_species", quote=quote), TEXT) == ("keep", "", ["unbacked_drop"])


def test_quotes_match_despite_case_and_hyphens():
    assert decide(ans(animals="other_species", quote="Retro-spective study of 19 CATS"),
                  TEXT)[:2] == ("off_topic", "O1")


def test_screen_end_to_end(tmp_path):
    records = [{"key": str(k), "title": f"title {k}", "abstract": "" if k == 3 else "in 19 cats",
                "subject_terms": "Cats"} for k in range(1, 4)]

    def respond(step, prompt, schema):
        assert step == "screen" and schema == SCHEMA
        keys = re.findall(r'"key": "(\d+)"', prompt)
        return {"records": [ans(animals="other_species", quote="19 cats") | {"key": k}
                            for k in keys if k != "2"]}             # record 2 goes unanswered

    fake = FakeGemini(respond)
    out = screen("feline_asthma", FELINE, records, LLM(fake, corpus=tmp_path), batch=2)
    assert [(r["key"], r["decision"], r["rule"], r["flags"]) for r in out] == [
        ("1", "off_topic", "O1", []),
        ("2", "keep", "", ["not_answered"]),
        ("3", "keep", "", ["title_only", "unbacked_drop"]),    # its quote isn't in the title
    ]
    assert len(fake.calls) == 2 and all("feline asthma" in c["system"] for c in fake.calls)


class CutOff(FakeGemini):
    """Answers the first call for each batch with text cut off mid-way."""
    def __init__(self, responder, cut_every_time=()):
        super().__init__(responder)
        self.seen, self.always = set(), set(cut_every_time)

    def generate(self, model, system, prompt, schema, thinking, max_output, step=""):
        text, usage, thoughts = super().generate(model, system, prompt, schema, thinking,
                                                 max_output, step)
        first = re.findall(r'"key": "(\d+)"', prompt)[0]
        if first in self.always or first not in self.seen:
            self.seen.add(first)
            return text[: len(text) // 2], usage, thoughts
        return text, usage, thoughts


def test_a_cut_off_answer_is_asked_again_then_kept_unanswered(tmp_path):
    records = [{"key": str(k), "title": "in 19 cats", "abstract": "in 19 cats", "subject_terms": ""}
               for k in range(1, 5)]

    def respond(step, prompt, schema):
        keys = re.findall(r'"key": "(\d+)"', prompt)
        return {"records": [ans(animals="other_species", quote="19 cats") | {"key": k} for k in keys]}
    fake = CutOff(respond, cut_every_time={"3"})
    out = screen("feline_asthma", FELINE, records, LLM(fake, corpus=tmp_path), batch=2, workers=1)
    assert [(r["key"], r["decision"], r["flags"]) for r in out] == [
        ("1", "off_topic", []), ("2", "off_topic", []),         # asked again, then answered
        ("3", "keep", ["not_answered"]), ("4", "keep", ["not_answered"])]
    assert len(fake.calls) == 4
