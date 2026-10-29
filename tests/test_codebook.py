"""codebook.csv and the topic files are internally consistent and match schema v2."""
import shutil
from pathlib import Path

import pytest

from vet.codebook import TABLES, Codebook

CB = Codebook()


def test_every_table_has_fields():
    for t in TABLES:
        assert CB.columns(t), t


def test_every_study_table_is_keyed_by_id():
    for t in TABLES:
        if t != "sources":
            assert "ID" in CB.columns(t), t


def test_coded_fields_need_quotes_except_bookkeeping():
    # schema v2 section 8: every filled C field needs a quote, except tables that carry their
    # own text (results, definitions) or are filled by code (sources, study_sources)
    exempt = {"sources", "study_sources", "results", "definitions", "evidence", "conflicts"}
    for f in CB.fields + [f for fields in CB.topic_fields.values() for f in fields.values()]:
        if f.kind == "C" and f.table not in exempt and f.name != "not_applicable":
            assert f.evidence == "quote", f"{f.table}.{f.name}"


def test_derived_fields_never_reach_templates():
    derived = {f.name for f in CB.fields if f.kind == "D"}
    assert derived
    for t in TABLES:
        assert not derived & set(CB.columns(t))


def test_topics_load_with_their_fields():
    assert {"surgical_pain", "imha", "cie"} <= set(CB.topics)
    for topic, fields in CB.topic_fields.items():
        assert CB.topics[topic].get("question"), topic
        assert fields, topic
        for f in fields.values():
            assert f.table == "topic_fields" and not f.applies, f"{topic}.{f.name}"
            assert f.name not in CB.columns("studies"), f"{topic}.{f.name} hides a core field"


def test_a_broken_topic_file_is_refused(tmp_path):
    for name in ("codebook.csv", "vocab/countries.txt"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        shutil.copy(Path(__file__).parent.parent / name, tmp_path / name)
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "x.toml").write_text(
        'question = "q"\n[[fields]]\nfield = "grade"\nkind = "C"\ntype = "enum"\n')
    with pytest.raises(ValueError, match="enum needs allowed_values"):
        Codebook(tmp_path / "codebook.csv")


def test_countries_are_the_apps_names():
    assert "United States" in CB.countries and "South Korea" in CB.countries
    assert "Korea, Republic of" not in CB.countries
    assert CB.field("studies", "Country").allowed == CB.countries


def test_not_applicable_lists_real_fields():
    allowed = CB.field("studies", "not_applicable").allowed
    assert "n_randomised" in allowed and "rescue_threshold" in allowed and "Name" not in allowed
