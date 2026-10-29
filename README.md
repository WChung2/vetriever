# Vetriever

Builds a cited database of clinical studies in animal patients (veterinary medicine,
not lab-animal research) from the published literature. First track: post-surgical pain in
dogs. Agents search and read; code checks every value. Each
value in the tables comes with a verbatim quote from its source document.

Status: **stage 1 (foundation) done.** The codebook, topics, the database with import and
export, the checker, the duplicate review, the literature connections and the Gemini wrapper
with its spending cap are in place. The Planner agent comes next (stage 2).

## How it fits together

```
 track brief ─▶ SEARCH ─▶ screen abstracts ─▶ REVIEW ─┐
                  ▲                                   │ new queries, references to follow
                  └───────────────────────────────────┘ until a round finds nothing new
                        │ you approve the list
                        ▼
                 STRUCTURE (one document at a time) ─▶ code checks ─▶ tables
                        │
                        ▼
                 REVIEW the gaps ─▶ export to the app's tables + gap report
```

- `codebook.csv`: one row per field. It drives the empty tables, the allowed values, the
  checks and the prompts. Change a field by editing its row, then run `vet templates`.
- `topics/<topic>.toml`: one file per topic a vet asks about: the question, search terms,
  screening rules, coverage targets, and the extra fields to extract for that topic.
- `settings.toml`: models, spending cap and prices. Secrets go in `.env` (copy
  `.env.example`).
- `codebook.md`: the readable version (generated, don't edit).
- `vocab/countries.txt`: country names, taken from the app's own ClinicalTrials.gov data.
- `templates/`: the 14 empty tables (header row only).
- `corpus/`: everything a run produces, kept local: `vetriever.db` (the database), `docs/`
  (original files and their text), `out/<source_id>/` (one folder per document), `merged/`
  (the database exported as CSV), `reports/`.

## The database

One SQLite file holds every table, and each document and study is stored once, whichever
topic found it.

- A document's key is set once, DOI first (`DOI-10-1111-VAA-12345`), and the document is found
  again by any of its DOI, PMID or PMCID, so the same paper is never stored twice.
- A study is `VET-<n>`, numbered as studies arrive and not built from any document, so it
  keeps its ID when more documents about it arrive.
- An import accounts for every value: stored, skipped (its document is already stored) or
  reported as dropped. A folder with a broken key is refused whole.
- `vet check` also lists possible duplicates for a person to review: documents with the same
  title, repeated rows, and studies in different documents that look like one trial.

## Commands

```
uv sync                         # set up the environment once
uv run vet templates            # rewrite templates/ and codebook.md from codebook.csv
uv run vet import <folder>      # add a folder of tables to the database
uv run vet check                # check the database, and list possible duplicates
uv run vet check corpus/out/PMID31234567   # check one document's folder
uv run vet export               # write the database out as CSV (corpus/merged)
uv run vet ping                 # one tiny live call to each literature service (free)
uv run pytest                   # tests (free: no API calls)
./scripts/check.sh              # everything CI checks: run before every push
```

## Checks (CI)

`scripts/check.sh` runs five checks, and GitHub Actions runs the same script on a clean
machine (Python 3.12 and 3.13) for every push and pull request:

1. installed packages match `uv.lock`;
2. lint (`ruff`);
3. tests, with the network blocked so no test can call a real service;
4. `templates/` and `codebook.md` match `codebook.csv`;
5. nothing private would be published: no `.env`, nothing from `corpus/` or `examples/`, no
   data file (the only tables allowed are `codebook.csv` and the empty `templates/`), no
   Google-style API key, and none of the values in your own `.env`.

## Sources and safety

Search uses scholarly and official services only: PubMed, Europe PMC, OpenAlex, Crossref,
Unpaywall, and the FDA and EMA veterinary pages. Code makes every call; the models only
write queries and fill fixed answer forms, so a web page can't make them do anything.
Requests are paced and identify the project. Gemini calls stop before passing the
spending cap in `settings.toml`, and every call can be audited in `corpus/calls/`.

## What is shared and what stays local

The repository holds the code, the codebook, the empty templates and the track briefs.
Everything a run produces stays in `corpus/`, which git ignores: the database itself is not
published, and the full texts in `corpus/docs/` include paywalled papers that may not be
redistributed. API keys go in `.env`, which git also ignores.

## Licence

The code is under the MIT licence (see `LICENSE`). The data the pipeline builds is not part
of this repository.
