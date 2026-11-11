# Vetriever

Builds a cited database of clinical studies in animal patients (veterinary medicine,
not lab-animal research) from the published literature. First track: post-surgical pain in
dogs. Agents search and read; code checks every value. Each
value in the tables comes with a verbatim quote from its source document.

Status: **stage 2 (the agents) in test.** On top of stage 1 (codebook, topics, database,
checker, literature connections, Gemini wrapper with its spending cap): the Planner, search,
screening, the search ⇄ review loop, the vet's list, full texts and who-and-where; the
extraction runner is being built.

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
  what screening looks for (`[screen]`), coverage targets, and the extra fields to extract
  for that topic.
- `prompts/`: the instructions given to the models. They name no species or disease: code
  fills in the topic's parts from `topics/<topic>.toml`.
- `settings.toml`: models, spending cap and prices. Secrets go in `.env` (copy
  `.env.example`).
- `codebook.md`: the readable version (generated, don't edit).
- `decisions.md` (kept on this computer, not published: it holds results of real runs): the calls made without a veterinary expert, listed for review.
- `vocab/countries.txt`: country names, taken from the app's own ClinicalTrials.gov data.
- `templates/`: the 14 empty tables (header row only).
- `corpus/`: everything a run produces, kept local: `vetriever.db` (the database), `docs/`
  (original files and their text), `text_before/` (texts as they were before `vet reread`),
  `out/<source_id>/` (one folder per document), `merged/` (the database exported as CSV),
  `reports/`.

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
uv run vet plan <topic> "<the vet's words>"   # Planner round 1 (paid); then --answers <file>, --start
uv run vet search <topic>       # one round of search and screening, then stop (paid); --budget <USD>
uv run vet search <topic> --widen   # the next round, as proposed: widening is your decision (paid)
uv run vet list <topic>         # the kept list for the vet; --approve-all approves it
uv run vet fetch <PMID or DOI>… # full texts into corpus/docs; --from <file>, or --topic <topic>
uv run vet reread               # read the stored PDFs again after the reader improves (free, local)
uv run vet who --topic <topic>  # authors and institutions of the approved papers (OpenAlex, free)
uv run vet ui                   # the screens in your browser, on this Mac only (free)
uv run vet ping                 # one tiny live call to each literature service (free)
uv run python scripts/tablebench.py packets <reader>…   # the table reader benchmark: blind packets, then `score`
uv run python scripts/golden_run.py <pdf|pypdf|mineru>  # the golden test: Gemini answers from one route (paid)
uv run pytest                   # tests (free: no API calls)
./scripts/check.sh              # everything CI checks: run before every push
./scripts/smoke.sh              # the real services on known papers (needs keys and VPN)
```

## Full texts

`vet fetch` tries legal routes from free to subscription: PubMed Central, a free copy listed
by OpenAlex, then the publisher's text-mining API (Elsevier with `ELSEVIER_API_KEY`, Wiley
with `WILEY_TDM_TOKEN`, or a link the publisher registered at Crossref, e.g. Springer).
The subscription routes use the institution's licence: they work only on its network or VPN
and only for non-commercial research. Publisher web pages are never scraped. A file is kept
only if it is the whole paper (no one-page previews) and the record's title is in it; the
rest is listed in `corpus/reports/to_download.csv` with a link, to download by hand.

Some PDFs draw symbols with fonts whose codes say nothing about what they show: the text layer
has "o" where the page shows "<", "9" for "×", "l" for "µ". `vet/pdftext.py` corrects these on
this computer, with no AI, from a table of what each font really draws, made by looking at the
glyph outlines in the fonts themselves. It corrects the font and not the text, so only the
symbols change. A code nobody has checked comes out as "�", never as a wrong letter. A paper
with unread characters or an unchecked symbol font gets a `text_warning`. `vet reread` applies
an improved reader to the stored PDFs and keeps each earlier text in `corpus/text_before/`.

## Checks (CI)

`scripts/check.sh` runs six checks, and GitHub Actions runs the same script on a clean
machine (Python 3.12 and 3.13) for every push and pull request:

1. installed packages match `uv.lock`;
2. lint (`ruff`);
3. tests, with the network blocked so no test can call a real service;
4. `templates/` and `codebook.md` match `codebook.csv`;
5. nothing private would be published: no `.env`, nothing from `corpus/` or `examples/`, no
   data file (the only tables allowed are `codebook.csv` and the empty `templates/`), no
   search asked in the chat (only the three example topics are published), no Google-style API
   key, and none of the values in your own `.env`;
6. the screens for real (`scripts/ui_smoke.py`), on made-up data in a temporary folder:
   - `vet ui` starts and every page answers with its security headers;
   - your machine's network address and other host names are refused;
   - htmx is the published release;
   - in headless Chrome no page has a blocked style or script, or a script error, including
     the Planner's form, the budget form, the widen panel and a running step's live progress
     (skipped where Chrome is missing);
   - a real click on Next from the bottom of a long list lands at the top of the next page,
     with the header still on screen;
   - a second `vet ui` on a taken port stops with a message.

   It stops only the server it started.

## Sources and safety

Search uses scholarly and official services only: PubMed, Europe PMC, OpenAlex, Crossref,
Unpaywall, and the FDA and EMA veterinary pages. Code makes every call; the models only
write queries and fill fixed answer forms, so a web page can't make them do anything.
Requests are paced and identify the project. Gemini calls stop before passing the
spending cap in `settings.toml`, and every call can be audited in `corpus/calls/`.

## The screens (`vet ui`)

A temporary local front end for one user, laid out as a chat. On the left is a dashboard of
searches, each with its five steps (plan, search, approve, full text, extract). On the right
is the chat, or a search's history in four tabs: the conversation (earlier command-line runs
rebuilt from their saved Planner rounds and logs), the records to approve, the studies built
(clinical_ai's essentials: status, species, condition, intervention, enrolled, sites, the KOLs
by name and the sponsor; inclusion and exclusion criteria; results and source text a click
away), and who and where. KOLs are a paper's first and last authors, or a trial's study
director; Who & where ranks institutions by the papers they led. The Review tab (your decisions:
approve, reject, or keep a dropped article anyway) opens with how
the search was built (the vet's words as typed, the agreed question, the expanded terms and
the exact queries run), every count explains itself, and each record says how it was found. It is a thin layer (`vet/ui/`,
Jinja templates and htmx) over plain functions in `vet/views.py` and `vet/chat.py`, so another
front end can reuse them. It loads nothing from the internet. It listens on 127.0.0.1 only,
answers only 127.0.0.1 or localhost, and accepts a change only from its own pages with their
token. Looking at a page opens the database read-only and spends nothing.

A question typed in the chat starts a search. The Planner asks about the scope and shows real
papers to mark; you revise or agree. You then set a budget and start the search. **Code never
widens a search on its own:** each round stops and shows what it found and cost, and what the
next round would do, and you choose to widen or finish. The budget is a hard limit: a round
starts only with its cost and a margin left, and the ledger refuses any call that could pass it.
A round cut off by the budget, an error or a stop carries on where it stopped, with nothing lost
or paid twice. Paid steps run in the background, one at a time (`vet/jobs.py`).

The Review list is made to be read from the top, not worked through. Each kept article shows a
confidence (high, medium or low) with its reason: how sure the screening is that it is on topic.
Code works it out from what the record shows, free, and no model grades itself. The list is
ordered by confidence, then by whether an article names a treatment from the question, then by
design, then newest first. One click approves the high-confidence articles. On Claude's sealed
benchmark verdicts, high was right 25 of 25 times, medium 17 of 18 and low 3 of 4.

Those paid steps are the only things that reach out, and only when you start one: what you
type and the papers' abstracts go to Gemini (paid tier), so don't type client or patient
details. A search asked in the chat stays on this Mac: its topic file is git-ignored, and check
step 5 refuses to publish one.

## What is shared and what stays local

The repository holds the code, the codebook, the empty templates and the track briefs.
Everything a run produces stays in `corpus/`, which git ignores: the database itself is not
published, and the full texts in `corpus/docs/` include paywalled papers that may not be
redistributed. API keys go in `.env`, which git also ignores.

## Licence

The code is under the MIT licence (see `LICENSE`). The data the pipeline builds is not part
of this repository.
