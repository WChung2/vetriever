"""Possible duplicates, for a person to look at. Nothing here is decided automatically.

The same paper stored twice (a shared DOI, PMID or PMCID) is an error and is caught by
validate.py (duplicate_document). These are only suggestions:
- possible_same_document: two documents with the same title and year;
- duplicate_row: two rows of one study that are identical apart from their key;
- possible_same_trial: studies in different documents that look like one trial reported
  twice (e.g. a paper and the FDA summary of the same field study). Matching them for real,
  with a person's confirmation, comes later; this list shows where to look.
"""
import re
from collections import defaultdict
from itertools import combinations

from .validate import PRIMARY_KEY, Issue

# the column that only numbers a row; two rows equal in everything else are a duplicate
# (breeds has none: its key is the breed itself, so the primary key already catches repeats)
NUMBERED = {"arms": "arm_id", "arm_doses": "dose_id", "results": "result_id",
            "sites": "site_no", "people": "person_no"}
TITLES = {"dvm", "phd", "msc", "bvsc", "bvms", "dvetmed", "dacvs", "dacvaa", "dacvim",
          "decvaa", "decvs", "decvim", "dipl", "diplomate", "mrcvs", "prof", "professor"}
# words in drug names that say nothing about which drug it is
NOT_A_DRUG = {
    "placebo", "saline", "sterile", "solution", "solutions", "tablet", "tablets", "chewable",
    "chewables", "injection", "injectable", "sodium", "chloride", "hydrochloride", "vehicle",
    "water", "control", "formulation", "topical", "transdermal", "liquid", "capsule",
    "capsules", "gelatin", "isotonic", "suspension", "powder", "infusion", "dextrose",
    "lactated", "ringer", "ringers", "commercial", "palatable", "treats", "standard", "group",
    "doses"}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z]{5,}", text.lower()) if w not in NOT_A_DRUG}


def _title(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _person(name: str) -> frozenset:
    """A name as its words, whatever the order: 'Watari Toshihiro' = 'Toshihiro Watari'."""
    return frozenset(w for w in re.findall(r"[a-z]{3,}", name.lower()) if w not in TITLES)


def _same_person(a: frozenset, b: frozenset) -> bool:
    return a == b or (min(len(a), len(b)) >= 2 and (a <= b or b <= a))


def possible_duplicates(t: dict) -> list[Issue]:
    issues = []

    seen = {}
    for i, r in enumerate(t["sources"]):
        key = (_title(r["title"]), r["year"])
        if key[0] and key in seen:
            issues.append(Issue("possible_same_document", "sources", i + 2, r["source_id"],
                                "title", f"same title and year as {seen[key]}"))
        seen.setdefault(key, r["source_id"])

    for table, number in NUMBERED.items():
        seen = {}
        for i, r in enumerate(t[table]):
            body = tuple((c, v) for c, v in r.items() if c != number)
            if body in seen:
                issues.append(Issue("duplicate_row", table, i + 2, "|".join(r[c] for c in
                                    PRIMARY_KEY[table]), "", f"same as line {seen[body]} "
                                    "apart from its key"))
            seen.setdefault(body, i + 2)

    issues += _same_trial(t)
    return issues


def _same_trial(t: dict) -> list[Issue]:
    primary = {r["ID"]: r["source_id"] for r in t["study_sources"] if r["role"] == "primary"}
    drugs, arm_sizes, people = defaultdict(set), defaultdict(list), defaultdict(list)
    for r in t["arm_doses"]:
        drugs[r["ID"]] |= _words(r["drug"])
    for r in t["arms"]:
        if r["n_assigned"]:
            arm_sizes[r["ID"]].append(r["n_assigned"])
    for r in t["people"]:
        if _person(r["Name"]):
            people[r["ID"]].append((_person(r["Name"]), r["Name"]))
    studies = {r["ID"]: r for r in t["studies"]}
    out = []
    for a, b in combinations(sorted(studies), 2):
        if primary.get(a) == primary.get(b):
            continue
        shared = drugs[a] & drugs[b]
        if not shared:
            continue
        signs = []
        if studies[a]["n_enrolled"] and studies[a]["n_enrolled"] == studies[b]["n_enrolled"]:
            signs.append(f"both enrolled {studies[a]['n_enrolled']}")
        if len(arm_sizes[a]) > 1 and sorted(arm_sizes[a]) == sorted(arm_sizes[b]):
            signs.append(f"same arm sizes {'/'.join(sorted(arm_sizes[a]))}")
        both = [name for p, name in people[a] if any(_same_person(p, q) for q, _ in people[b])]
        if len(both) >= 2:
            signs.append(f"{len(both)} people in both (e.g. {', '.join(both[:3])})")
        if signs:
            out.append(Issue("possible_same_trial", "studies", 0, f"{a}|{b}", "",
                             f"{primary.get(a)} and {primary.get(b)}: same drug "
                             f"({', '.join(sorted(shared))}); " + "; ".join(signs)))
    return out
