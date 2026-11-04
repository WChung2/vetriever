"""Live smoke test: the real services, on fixed IMHA papers whose answers we know. Skipped
unless VET_LIVE=1, so CI and the normal test run stay offline; run it with scripts/smoke.sh.
Each pipeline step gets a check here when it is built.
"""
import csv
import os

import pytest

from vet import connectors as c
from vet.fetch import fetch
from vet.settings import env

pytestmark = [pytest.mark.enable_socket,
              pytest.mark.skipif(not os.environ.get("VET_LIVE"), reason="live: scripts/smoke.sh")]

DOGS = '("Dogs"[Mesh] OR dog[tiab] OR dogs[tiab] OR canine[tiab])'
IMHA = ('("Anemia, Hemolytic, Autoimmune"[Mesh] OR "Autoimmune Hemolytic Anemia"[tiab] OR '
        '"immune-mediated haemolytic anaemia"[tiab] OR "immune-mediated hemolytic anemia"[tiab] OR '
        '"immune mediated hemolytic anemia"[tiab] OR "immune-mediated hemolytic anaemia"[tiab] OR '
        'IMHA[tiab])')
# Claude Science's IMHA studies: the topic search must find every one
KNOWN = {"21489250", "26473338", "31888626", "33571376", "34231261", "34779044", "35801263",
         "36809664", "37505830", "38308396", "38961558", "39054564"}
# one paper per route, and the route each must end on (on the EPFL network or VPN)
PAPERS = {"38961558": "pmc",          # JVIM 2024, PubMed Central
          "18083185": "elsevier",     # J Comp Pathol 2008, subscription
          "19691510": "wiley",        # J Vet Emerg Crit Care 2009, subscription
          "1441221": "",              # Vet Immunol Immunopathol 1992: one-page preview only
          "11330619": ""}             # JAVMA 2001: no text-mining route, so by hand


def test_layer0_topic_search_finds_the_known_studies():
    n, pmids = c.pubmed_search(c.Http(), f"{DOGS} AND {IMHA}", 1000)
    assert n >= 400, f"only {n} hits: did PubMed change a MeSH term?"
    assert KNOWN <= set(pmids), f"missed {sorted(KNOWN - set(pmids))}"


def test_fetch_ends_each_paper_on_its_route(tmp_path):
    missing = [k for k in ("ELSEVIER_API_KEY", "WILEY_TDM_TOKEN") if not env(k)]
    if missing:
        pytest.skip(f"needs {', '.join(missing)} in .env")
    papers = fetch(list(PAPERS), tmp_path, say=lambda s: None)
    got = {p.pmid: p.route for p in papers}
    why = {p.pmid: p.tried for p in papers}
    for pmid, route in PAPERS.items():
        assert got[pmid] == route, f"PMID {pmid}: wanted {route or 'none'}, got " \
            f"{got[pmid] or 'none'} ({why[pmid]}); on the EPFL network or VPN?"
    assert "preview only" in why["1441221"]
    for p in papers:
        if p.route:
            assert float(p.title_match) >= 0.8 and (tmp_path / p.text_file).stat().st_size > 3000
    with open(tmp_path / "reports" / "to_download.csv", encoding="utf-8") as fh:
        assert {r["pmid"] for r in csv.DictReader(fh)} == {"1441221", "11330619"}
