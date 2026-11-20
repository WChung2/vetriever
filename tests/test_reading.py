"""The careful reader: an article is read sentence by sentence and cell by cell, nothing is
dropped by judgement, and a stored study's reading can be replaced in place."""
import copy
import json

from tests import fixture
from vet.codebook import Codebook
from vet.llm import LLM, FakeGemini
from vet.reading import Reading, criteria_layout, parts, printed, reread, same_sentences, sentences
from vet.store import Store

CB = Codebook()
# Every text and number below is made up: the repository is public, the data is not.
ARTICLE = """## Abstract
Nine cats were treated. In summary, appetite at 10 days was 55.5% in the treated group (p = 0.321).

## Results
Appetite at 10 days was 55.5% in the treated group (p = 0.321). No adverse events were seen.

Table 1
Appetite by group.
 | Treated | Control | P |
Eating at day 10 | 5/9 | 3/11 | 0.321 |
Body weight | Median | - | |

## References
1. Somebody. A paper. 2001.
"""


def test_a_paragraph_is_cut_into_sentences_without_losing_a_word():
    text = ("Values were 7.1 (IQR, 6.2–8.3), e.g. at entry. All cats were weighed.12 In total, "
            "30 visits were recorded. Eleven of 40 cats (28%) had 1.")
    cut = sentences(text)
    assert cut == ["Values were 7.1 (IQR, 6.2–8.3), e.g. at entry.", "All cats were weighed.12",
                   "In total, 30 visits were recorded.", "Eleven of 40 cats (28%) had 1."]
    assert " ".join(cut) == text


def test_the_article_becomes_parts_and_the_reference_list_is_left_out():
    got = parts(ARTICLE, pdf=False)
    assert [p["kind"] for p in got] == ["text", "text", "table"]
    assert got[2]["text"].startswith("Table 1\nAppetite by group.")
    assert not any("Somebody" in p["text"] for p in got)


def test_the_same_sentence_is_read_once():
    a, b = parts(ARTICLE, pdf=False)[:2]
    same = same_sentences([[a], [b]])
    assert same == {"P2.1": "P1.2"}               # the abstract's copy carries a lead-in


def test_a_p_value_is_kept_only_when_it_is_printed_there():
    assert printed("0.321", ["appetite was 55.5% (chi-squared test, p = 0.321)"])
    assert printed("<0.001", ["differed (P < .001; Table 5)"])
    assert not printed(">=0.05", ["did not differ significantly"])
    assert not printed("none", ["Body weight 4.1 3.9 5.2"])
    assert not printed("<0.0001", ["No adverse events were seen"])


def test_entry_criteria_are_put_in_the_layout_without_touching_their_words():
    got = criteria_layout("Inclusion criteria:\n- weight loss (more than 10%)\nExclusion criteria\n* another illness")
    assert got == "Inclusion Criteria:\n* weight loss (more than 10%)\nExclusion Criteria:\n* another illness"
    assert criteria_layout("cats losing weight") == "Inclusion Criteria:\n* cats losing weight"


def answers(step, prompt, schema):
    """A model that sorts every part under findings and gives one row per request: for the
    sentence that prints the p-value (else the first), or for the first cell; everything else it
    lists as holding no result."""
    props = schema["properties"]
    if "parts" in props:
        return {"parts": [{"id": i, "sections": ["findings"]} for i in props["parts"]["items"]["properties"]["id"]["enum"]]}
    if "no_result" in props:
        ids = props["no_result"]["items"]["enum"]
        row = {"result_kind": "outcome", "endpoint": "appetite at 10 days", "group": "all animals",
               "value": 55.5, "value_unit": "%", "p_value": "0.321",
               "borrowed": [{"location": "P1", "quote": "Nine cats were treated."}]}
        if "sentence" in props["results"]["items"]["properties"]:
            shown = json.loads(prompt[prompt.index("## Paragraphs to read\n") + 22:prompt.index("\n\n## This request")])
            text = {s["id"]: s["text"] for p in shown for s in p["sentences"]}
            pick = next((i for i in ids if "0.321" in text[i]), ids[0])
            return {"results": [row | {"sentence": pick, "quote": text[pick]}],
                    "no_result": [i for i in ids if i != pick]}
        return {"results": [row | {"cells": ids[:1]}], "no_result": ids[1:]}
    if "arms" in props:
        return {"arms": [{"evidence": [{"location": "P1", "quote": "Nine cats were treated."}],
                          "arm_label": "treated", "n_assigned": 9}]}
    if "studies" in props:
        return {"studies": {"n_enrolled": {"evidence": [{"location": "P1", "quote": "Nine cats were treated."}],
                                           "value": 9}}}
    return {name: {"evidence": [{"location": "P2", "quote": "No adverse events were seen."}], "value": v}
            for name, v in (("Study Type", "OBSERVATIONAL"), ("tests_treatment", "no"), ("results_reported", "yes"),
                            ("comparison_or_goal", "no"), ("low_power", "no"), ("main_outcome", "met"),
                            ("called_primary", "no"))}


def test_a_document_is_read_into_tables_with_every_sentence_and_cell_answered(tmp_path):
    llm = LLM(FakeGemini(answers), save_calls=False, corpus=tmp_path)
    tables, stats = Reading("VET-1", "PMID-1", ARTICLE, llm, CB, pdf=False).run()
    (study,) = tables["studies"]
    assert study["n_enrolled"] == "9" and study["Study Type"] == "OBSERVATIONAL"
    assert stats["text"]["unanswered"] == 0 and stats["tables"]["cells_left_open"] == 0
    assert stats["text"]["same_sentences"] == 1
    sentence, cell = tables["results"]
    assert sentence["as_written"].startswith("In summary, appetite at 10 days was 55.5%")
    assert sentence["p_value"] == "0.321" and sentence["location"] == "Abstract"    # printed in that sentence
    assert cell["as_written"].startswith("| Treated | Control | P | … Eating at day 10")
    assert cell["p_value"] == ""                  # the cell it names holds 5/9, not the p-value
    assert [r["result_id"] for r in tables["results"]] == ["VET-1-R1", "VET-1-R2"]
    assert {(e["table"], e["field"]) for e in tables["evidence"]} >= {("studies", "n_enrolled"), ("arms", "n_assigned")}


def stored(tmp_path) -> Store:
    store = Store(tmp_path / "vetriever.db", CB)
    store.import_tables(fixture.clean(CB))
    return store


def test_a_study_read_again_keeps_its_id_its_people_and_its_groups(tmp_path):
    store = stored(tmp_path)
    before = store.tables()
    (study_id,) = {r["ID"] for r in before["studies"]}
    arms = sorted(before["arms"], key=lambda r: r["arm_id"])
    new_arms = [{"ID": study_id, "arm_id": f"{study_id}-A1", "arm_label": arms[-1]["arm_label"], "n_assigned": "9"},
                {"ID": study_id, "arm_id": f"{study_id}-A2", "arm_label": "a group not seen before"}]
    result = {"ID": study_id, "source_id": before["results"][0]["source_id"], "result_id": f"{study_id}-R1",
              "arm_id": f"{study_id}-A1", "result_kind": "outcome", "endpoint": "appetite",
              "as_written": "Appetite was 55.5%.", "location": "Results", "value": "55.5"}
    study = copy.deepcopy(before["studies"][0]) | {"n_enrolled": "12"}
    counts = store.replace_reading(study_id, {"studies": [study], "arms": new_arms, "arm_doses": [],
                                              "results": [result], "sites": [], "evidence": []})
    after = store.tables()
    assert [r["ID"] for r in after["studies"]] == [study_id] and after["studies"][0]["n_enrolled"] == "12"
    assert after["people"] == before["people"] and after["sources"] == before["sources"]
    assert after["study_sources"] == before["study_sources"]
    kept = next(r for r in after["arms"] if r["arm_label"] == arms[-1]["arm_label"])
    assert kept["arm_id"] == arms[-1]["arm_id"]                       # the same name keeps its id
    assert after["results"][0]["arm_id"] == arms[-1]["arm_id"]         # and its results follow it
    assert len(after["results"]) == 1 and counts["results"] == 1
    assert all(r["arm_id"] in {a["arm_id"] for a in after["arms"]} | {""} for r in after["breeds"])
    store.close()


def test_stored_studies_are_read_again_in_place_and_a_stopped_run_goes_on(tmp_path):
    store = stored(tmp_path)
    (study_id,) = {r["ID"] for r in store.rows("studies")}
    (sid,) = {r["source_id"] for r in store.rows("study_sources") if r["role"] == "primary"}
    store.db.execute("UPDATE sources SET text_file = '', file_name = '' WHERE source_id = ?", (sid,))
    store.db.commit()
    store.close()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / f"{sid}.txt").write_text(ARTICLE, "utf-8")
    llm = LLM(FakeGemini(answers), save_calls=False, corpus=tmp_path)
    said = []
    done = reread([study_id, "VET-999"], llm, tmp_path / "vetriever.db", say=said.append, workers=1)
    assert done == {"read": 1, "failed": 0, "skipped": 1, "already done": 0, "stored from a kept reading": 0}
    assert (tmp_path / "readings" / f"{study_id}.json").is_file()      # the reading itself is kept on disk
    store = Store(tmp_path / "vetriever.db", CB)
    assert len(store.rows("results")) == 2 and store.rows("studies")[0]["ID"] == study_id
    assert (tmp_path / "notes" / f"{sid}.json").is_file()              # sorted once, kept
    store.close()
    again = reread([study_id], llm, tmp_path / "vetriever.db", say=said.append, workers=1)
    assert again["already done"] == 1 and again["read"] == 0


def test_a_reading_that_could_not_be_stored_is_kept_and_stored_later_without_asking_again(tmp_path, monkeypatch):
    store = stored(tmp_path)
    (study_id,) = {r["ID"] for r in store.rows("studies")}
    (sid,) = {r["source_id"] for r in store.rows("study_sources") if r["role"] == "primary"}
    store.db.execute("UPDATE sources SET text_file = '', file_name = '' WHERE source_id = ?", (sid,))
    store.db.commit()
    store.close()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / f"{sid}.txt").write_text(ARTICLE, "utf-8")
    fake = FakeGemini(answers)
    llm = LLM(fake, save_calls=False, corpus=tmp_path)
    real = Store.replace_reading

    def refuse(self, study_id, tables):
        raise ValueError("the database is busy")

    monkeypatch.setattr(Store, "replace_reading", refuse)
    said = []
    first = reread([study_id], llm, tmp_path / "vetriever.db", say=said.append, workers=1)
    assert first["read"] == 0 and first["failed"] == 1 and "kept in" in said[-1]
    asked = len(fake.calls)
    monkeypatch.setattr(Store, "replace_reading", real)
    second = reread([study_id], llm, tmp_path / "vetriever.db", say=said.append, workers=1)
    assert second["stored from a kept reading"] == 1 and len(fake.calls) == asked     # nothing asked again
    store = Store(tmp_path / "vetriever.db", CB)
    assert len(store.rows("results")) == 2
    store.close()


def test_a_breed_whose_group_is_gone_stays_with_the_study(tmp_path):
    store = stored(tmp_path)
    before = store.tables()
    (study_id,) = {r["ID"] for r in before["studies"]}
    arm = before["arms"][0]["arm_id"]
    store.db.execute("DELETE FROM breeds")
    store.db.executemany("INSERT INTO breeds (ID, arm_id, Breed, n_dogs) VALUES (?, ?, ?, ?)",
                         [(study_id, arm, "Maine Coon", "3"), (study_id, "", "Maine Coon", "5"),
                          (study_id, arm, "Siamese", "2")])
    store.db.commit()
    study = copy.deepcopy(before["studies"][0])
    new_arms = [{"ID": study_id, "arm_id": f"{study_id}-A1", "arm_label": "first new group"},
                {"ID": study_id, "arm_id": f"{study_id}-A2", "arm_label": "second new group"},
                {"ID": study_id, "arm_id": f"{study_id}-A3", "arm_label": "third new group"}]
    counts = store.replace_reading(study_id, {"studies": [study], "arms": new_arms, "arm_doses": [],
                                              "results": [], "sites": [], "evidence": []})
    breeds = {(r["arm_id"], r["Breed"], r["n_dogs"]) for r in store.rows("breeds")}
    assert breeds == {("", "Maine Coon", "5"), ("", "Siamese", "2")}
    assert counts["breeds_set_to_the_whole_study"] == 1
    assert counts["breeds_already_there_for_the_whole_study"] == 1
    store.close()
