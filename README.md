# Vetriever

Builds a cited database of clinical studies in animal patients (veterinary medicine,
not lab-animal research) from the published literature. First track: post-surgical pain in
dogs. Agents search and read; code checks every value. Each
value in the tables comes with a verbatim quote from its source document.

Status: **Phase 0 (setup) done.** The codebook, the empty tables, the checker, the
literature connections, the Gemini wrapper with its spending cap, and the draft pain brief
are in place. The search, reading and export agents come next (Phase 1: pilot).

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
- `tracks/<track>.toml`: what each track looks for (screening rules, coverage targets,
  starting queries).
- `settings.toml`: models, spending cap and prices. Secrets go in `.env` (copy
  `.env.example`).
- `codebook.md`: the readable version (generated, don't edit).
- `vocab/countries.txt`: country names, taken from the app's own ClinicalTrials.gov data.
- `templates/`: the 14 empty tables (header row only).
- `corpus/`: everything a run produces: `docs/` (original files and their text, kept local),
  `sources.csv`, `study_sources.csv`, `out/<source_id>/` (one folder per document),
  `merged/`, `reports/`.

## Commands

```
uv sync                         # set up the environment once
uv run vet templates            # rewrite templates/ and codebook.md from codebook.csv
uv run vet check                # check corpus/merged against the codebook
uv run vet check corpus/out/PMID31234567   # check one document's folder
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
5. nothing private would be published: no `.env`, nothing from `corpus/`, no Google-style
   API key, and none of the values in your own `.env`.

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
