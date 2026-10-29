"""A hand-built, fictional fixture (one study, two documents) that passes every check.
Tests break it one way at a time. Evidence rows are added for every field that needs one,
so a fixture edit can't forget them."""
import csv
from pathlib import Path

from vet.codebook import TABLES, Codebook
from vet.validate import ROW_KEY

SID = "VET-PMID99999901-1"
A1, A2 = f"{SID}-A1", f"{SID}-A2"


def _row(cb, table, /, **values):
    cols = cb.columns(table)
    unknown = set(values) - set(cols)
    assert not unknown, f"{table} has no {unknown}"
    return {c: str(values.get(c, "")) for c in cols}


def clean(cb: Codebook) -> dict:
    t = {name: [] for name in TABLES}
    t["sources"] = [
        _row(cb, "sources", source_id="PMID99999901", doc_type="journal_article",
             title="FIXTURE: fictional analgesic trial", first_author="Doe J", last_author="Roe R",
             year=2020, journal_or_agency="Fictional Vet J", doi="10.9999/fixture.1",
             pmid=99999901, language="en", access="open_access",
             file_name="docs/PMID99999901.xml", text_file="docs/PMID99999901.txt",
             retrieved_date="2026-10-28"),
        _row(cb, "sources", source_id="FDA-NADA999999", doc_type="fda_foi_summary",
             title="FIXTURE: fictional FOI summary", year=2021, journal_or_agency="FDA CVM",
             language="en", access="open_access", file_name="docs/FDA-NADA999999.pdf",
             text_file="docs/FDA-NADA999999.txt", retrieved_date="2026-10-28"),
    ]
    t["study_sources"] = [
        _row(cb, "study_sources", ID=SID, source_id="PMID99999901", role="primary"),
        _row(cb, "study_sources", ID=SID, source_id="FDA-NADA999999", role="regulatory"),
    ]
    t["studies"] = [_row(
        cb, "studies", ID=SID, indication="surgical_pain", study_kind="prospective_trial",
        study_stage="dose_determination", **{"Study Type": "INTERVENTIONAL",
                                             "Overall Status": "COMPLETED",
                                             "Started date": "2019-03"},
        disease_name="postoperative pain", keywords="analgesia|dog",
        Summary="To compare two doses of analgesic X after ovariohysterectomy.",
        **{"Eligibility Criteria": "Inclusion Criteria:\n\n* Dogs undergoing ovariohysterectomy"
                                   "\n\nExclusion Criteria:\n\n* Pregnant dogs"},
        n_enrolled=60, n_randomised=60, n_analysed=58, age_min_value=6, age_min_unit="months",
        age_as_written="at least 6 months old", weight_min_value=5, weight_min_unit="kg",
        species="dog", Sex="FEMALE", intervention_type="DRUG", control_type="placebo",
        allocation="parallel", randomised="yes", blinding="double", outcome_instruments="CMPS-SF",
        Sponsor="Fictional Pharma", Country="Switzerland|United States", trial_outcome="positive",
        outcome_basis="primary_endpoint")]
    t["arms"] = [
        _row(cb, "arms", ID=SID, arm_id=A1, arm_label="placebo", is_control="yes",
             n_assigned=30, n_analysed=29),
        _row(cb, "arms", ID=SID, arm_id=A2, arm_label="analgesic X", is_control="no",
             n_assigned=30, n_analysed=29, arm_as_written="analgesic X 2 mg/kg SC"),
    ]
    t["arm_doses"] = [_row(cb, "arm_doses", ID=SID, arm_id=A2, dose_id=f"{A2}-D1",
                           drug="analgesic X", dose_value=2, dose_unit="mg/kg", route="SC",
                           frequency="once", timing="at extubation",
                           dose_as_written="2 mg/kg SC once at extubation")]
    t["results"] = [
        _row(cb, "results", result_id=f"{SID}-R1", ID=SID, source_id="PMID99999901", arm_id=A2,
             endpoint="rescue analgesia", timepoint_value=24, timepoint_unit="hours",
             timepoint_as_written="24 h", n=29, events=3, statistic_type="count", value=3,
             as_written="3/29 dogs given analgesic X needed rescue within 24 h", location="Results"),
        _row(cb, "results", result_id=f"{SID}-R2", ID=SID, source_id="FDA-NADA999999", arm_id=A2,
             endpoint="rescue analgesia", n=29, events=3, statistic_type="risk_ratio", value=0.3,
             variability_type="CI95", variability_low=0.1, variability_high=0.9,
             comparison="analgesic X vs placebo", reference_arm_id=A1, p_value="<0.05",
             as_written="RR 0.3 (95% CI 0.1-0.9), p<0.05", location="Table 3"),
    ]
    t["sites"] = [_row(cb, "sites", ID=SID, site_no=1, Facility="Fictional Veterinary Hospital",
                       City="Bern", Country="Switzerland")]
    t["people"] = [
        _row(cb, "people", ID=SID, source_id="PMID99999901", person_no=1, Name="Doe J",
             Affiliation="Fictional Veterinary Hospital", role="author", author_position="first",
             is_corresponding="yes", site_no=1),
        _row(cb, "people", ID=SID, source_id="PMID99999901", person_no=2, Name="Roe R",
             role="author", author_position="last", is_corresponding="no"),
    ]
    t["breeds"] = [_row(cb, "breeds", ID=SID, Breed="mixed breed", n_dogs=20)]
    t["definitions"] = [_row(cb, "definitions", source_id="PMID99999901", ID=SID, term="rescue",
                             definition_verbatim="CMPS-SF score of 6 or more",
                             location="Methods")]
    t["ext_pain"] = [_row(cb, "ext_pain", ID=SID, surgery_types="ovariohysterectomy",
                          surgery_category="soft_tissue",
                          anesthesia_protocol="propofol induction, isoflurane maintenance")]
    t["evidence"] = evidence_for(cb, t)
    return t


def evidence_for(cb: Codebook, t: dict) -> list:
    rows = []
    for table, cols in ROW_KEY.items():
        for row in t[table]:
            for f in cb.tables[table]:
                if f.evidence == "none" or not row.get(f.name):
                    continue
                quote = f"fixture quote for {f.name}" if f.evidence == "quote" else ""
                rows.append(_row(cb, "evidence", ID=row["ID"], source_id="PMID99999901",
                                 table=table, row_key="|".join(row.get(c, "") for c in cols),
                                 field=f.name, value=row[f.name], quote=quote,
                                 quote_truncated="no", location="Methods"))
    return rows


def write(tables: dict, cb: Codebook, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with open(folder / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cb.columns(name), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
