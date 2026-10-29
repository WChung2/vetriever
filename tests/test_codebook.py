"""codebook.csv is internally consistent and matches schema v2."""
from vet.codebook import EXT, TABLES, Codebook

CB = Codebook()


def test_all_14_tables_have_fields():
    assert len(TABLES) == 14
    for t in TABLES:
        assert CB.columns(t), t


def test_every_study_table_is_keyed_by_id():
    for t in TABLES:
        if t != "sources":
            assert "ID" in CB.columns(t), t


def test_coded_fields_need_quotes_except_bookkeeping():
    # schema v2 section 8: every filled C field needs a quote, except tables that carry their
    # own text (results, definitions) or are filled by code (sources, study_sources)
    exempt = {"sources", "study_sources", "results", "definitions", "evidence"}
    for f in CB.fields:
        if f.kind == "C" and f.table not in exempt and f.name != "not_applicable":
            assert f.evidence == "quote", f"{f.table}.{f.name}"


def test_derived_fields_never_reach_templates():
    derived = {f.name for f in CB.fields if f.kind == "D"}
    assert derived
    for t in TABLES:
        assert not derived & set(CB.columns(t))


def test_extension_tables_apply_to_their_indication():
    for indication, table in EXT.items():
        for f in CB.tables[table]:
            assert f.applies == (("indication", (indication,)),), f"{table}.{f.name}"


def test_countries_are_the_apps_names():
    assert "United States" in CB.countries and "South Korea" in CB.countries
    assert "Korea, Republic of" not in CB.countries
    assert CB.field("studies", "Country").allowed == CB.countries


def test_not_applicable_lists_real_fields():
    allowed = CB.field("studies", "not_applicable").allowed
    assert "n_randomised" in allowed and "rescue_threshold" in allowed and "Name" not in allowed
