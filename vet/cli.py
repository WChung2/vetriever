"""`vet <command>`: every step is its own command and can be re-run alone."""
import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path

from .codebook import TABLES, Codebook, write_markdown, write_templates
from .settings import CORPUS, DB, ROOT
from .validate import load_folder, validate, write_report


def cmd_templates(args):
    cb = Codebook()
    if not args.check:
        write_templates(cb)
        write_markdown(cb)
        print(f"wrote templates/ ({len(TABLES)} tables) and codebook.md")
        return
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp)
        write_templates(cb, fresh / "templates")
        write_markdown(cb, fresh / "codebook.md")
        wanted = {p.relative_to(fresh) for p in fresh.rglob("*") if p.is_file()}
        have = {p.relative_to(ROOT) for p in (ROOT / "templates").glob("*.csv")}
        stale = sorted(str(p) for p in wanted if not (ROOT / p).exists()
                       or (ROOT / p).read_bytes() != (fresh / p).read_bytes())
        stale += sorted(str(p) for p in have - wanted)
    if stale:
        sys.exit(f"out of date with codebook.csv: {', '.join(stale)}\n"
                 "run 'uv run vet templates' and commit the result")
    print("templates/ and codebook.md match codebook.csv")


def cmd_check(args):
    if args.folder:
        folder = Path(args.folder).resolve()
        if not folder.is_dir():
            sys.exit(f"no such folder: {folder}")
        source_id = folder.name if folder.parent.name == "out" else None
        tables, headers = load_folder(folder, CORPUS)
        reports, name = CORPUS / "reports", source_id or folder.name
        scope = f"document {source_id}" if source_id else f"folder {folder.name}"
    else:
        from .duplicates import possible_duplicates
        from .store import Store
        store = Store(args.db)
        tables, headers, source_id = store.tables(), None, None
        reports, name, scope = args.db.parent / "reports", "database", str(args.db)
        review = possible_duplicates(tables)
        write_report(review, reports / "duplicates_to_review.csv")
        print(f"to review: {len(review)} possible duplicates; list in "
              f"{reports / 'duplicates_to_review.csv'}")
        for check, n in Counter(i.check for i in review).most_common():
            print(f"  {n:5d}  {check}")
    issues = validate(tables, Codebook(), source_id=source_id, headers=headers)
    report = reports / f"check_{name}.csv"
    write_report(issues, report)
    rows = sum(len(v) for v in tables.values())
    if not issues:
        print(f"OK: {rows} rows checked ({scope}), no problems")
        return
    print(f"{len(issues)} problems in {rows} rows ({scope}); details in {report}")
    for check, n in Counter(i.check for i in issues).most_common():
        print(f"  {n:5d}  {check}")
    sys.exit(1)


def cmd_import(args):
    from .store import Store
    store = Store(args.db)
    try:
        rep = store.import_folder(Path(args.folder))
    except ValueError as e:
        sys.exit(str(e))
    ids = list(rep.new_studies.values())
    print(f"imported {args.folder} into {args.db}")
    print(f"  documents: {len(rep.new_documents)} new, {len(rep.known_documents)} already "
          "stored (skipped with their studies)")
    print(f"  studies:   {len(ids)} new" + (f" ({ids[0]} to {ids[-1]})" if ids else "")
          + (f"; {len(rep.topic_from_document)} got their topic from their document"
             if rep.topic_from_document and ids else ""))
    print(f"  {'table':14s} {'in':>6s} {'added':>6s} {'skipped':>8s}")
    for t in TABLES:
        if rep.rows_in[t]:
            print(f"  {t:14s} {rep.rows_in[t]:6d} {rep.rows_added[t]:6d} {rep.rows_skipped[t]:8d}")
    for (table, column), n in sorted(rep.dropped.items()):
        print(f"  dropped: {table}.{column} ({n} filled cells): not in the codebook")
    print(f"  values: {rep.values_in} in = {rep.values_stored} stored + {rep.values_skipped} "
          f"skipped + {rep.values_dropped} dropped")
    if rep.missing:
        sys.exit(f"MISSING: {sum(rep.missing.values())} values did not arrive, e.g. "
                 f"{list(rep.missing)[:3]}")
    print("  every value accounted for")


def cmd_export(args):
    from .store import Store
    n = Store(args.db).export(Path(args.folder))
    print(f"wrote {n} rows in {len(TABLES)} tables to {args.folder}")


def cmd_ping(_args):
    """One tiny live call per literature service (free; nothing is saved)."""
    from . import connectors as c
    from .settings import env
    http = c.Http()
    pmid = doi = ""

    def pubmed():
        nonlocal pmid, doi
        n, ids = c.pubmed_search(http, '"Dogs"[Mesh] AND "Postoperative Pain"[Mesh]', 1)
        rec = c.pubmed_records(http, ids)[0]
        pmid, doi = rec.pmid, rec.doi
        return f"{n} hits; e.g. PMID {rec.pmid}: {rec.title[:60]}"

    def openalex():
        n, recs = c.openalex_works(http, 1, search="dog postoperative analgesia")
        key = "with key" if env("OPENALEX_API_KEY") else "no key: small demo allowance only"
        return f"{n} hits ({key}); e.g. {recs[0].title[:50]}"

    def unpaywall():
        if not env("CONTACT_EMAIL"):
            return "skipped: needs CONTACT_EMAIL in .env"
        return c.unpaywall_url(http, doi) or "no open-access copy for that DOI"

    checks = [
        ("PubMed", pubmed),
        ("PubMed similar", lambda: f"{len(c.pubmed_similar(http, pmid, 5))} similar articles"),
        ("PMC ID converter", lambda: str(c.convert_ids(http, [pmid]).get(pmid))),
        ("Europe PMC", lambda: f"{c.europepmc_search(http, '(dog OR canine) AND \"postoperative pain\"', 1)[0]} hits"),
        ("OpenAlex", openalex),
        ("Crossref", lambda: "DOI found" if c.crossref_work(http, doi) else "DOI NOT FOUND"),
        ("Unpaywall", unpaywall),
    ]
    failed = 0
    for name, run in checks:
        try:
            print(f"  ok    {name:17s} {run()}")
        except Exception as e:  # noqa: BLE001 — report every service, then fail at the end
            failed += 1
            print(f"  FAIL  {name:17s} {type(e).__name__}: {e}")
    print(f"NCBI key: {'set' if env('NCBI_API_KEY') else 'not set (3 requests/s)'}; "
          f"contact email: {'set' if env('CONTACT_EMAIL') else 'not set'}")
    if failed:
        sys.exit(1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="vet", description="Veterinary study database pipeline.")
    p.add_argument("--db", type=Path, default=DB, help=f"the database file (default {DB})")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("templates", help="write templates/ and codebook.md from codebook.csv")
    t.add_argument("--check", action="store_true",
                   help="only check they are up to date (CI); change nothing")
    t.set_defaults(run=cmd_templates)
    c = sub.add_parser("check", help="check the database (or a folder) against the codebook")
    c.add_argument("folder", nargs="?", help="a folder of tables, e.g. corpus/out/<source_id>; "
                   "default: the database, plus a list of possible duplicates to review")
    c.set_defaults(run=cmd_check)
    i = sub.add_parser("import", help="add a folder of tables to the database")
    i.add_argument("folder")
    i.set_defaults(run=cmd_import)
    e = sub.add_parser("export", help="write the database out as CSV tables")
    e.add_argument("folder", nargs="?", default=str(CORPUS / "merged"))
    e.set_defaults(run=cmd_export)
    sub.add_parser("ping", help="one tiny live call per literature service"
                   ).set_defaults(run=cmd_ping)
    args = p.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
