"""`vet <command>`: every step is its own command and can be re-run alone."""
import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path

from .codebook import Codebook, write_markdown, write_templates
from .settings import CORPUS, ROOT
from .validate import load_folder, validate, write_report


def cmd_templates(args):
    cb = Codebook()
    if not args.check:
        write_templates(cb)
        write_markdown(cb)
        print("wrote templates/ (14 tables) and codebook.md")
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
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        sys.exit(f"no such folder: {folder}")
    source_id = folder.name if folder.parent.name == "out" else None
    tables, headers = load_folder(folder, CORPUS)
    issues = validate(tables, Codebook(), source_id=source_id, headers=headers)
    report = CORPUS / "reports" / f"check_{source_id or folder.name}.csv"
    write_report(issues, report)
    rows = sum(len(v) for v in tables.values())
    scope = f"document {source_id}" if source_id else "merged set"
    if not issues:
        print(f"OK: {rows} rows checked ({scope}), no problems")
        return
    print(f"{len(issues)} problems in {rows} rows ({scope}); details in {report}")
    for check, n in Counter(i.check for i in issues).most_common():
        print(f"  {n:5d}  {check}")
    sys.exit(1)


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
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("templates", help="write templates/ and codebook.md from codebook.csv")
    t.add_argument("--check", action="store_true",
                   help="only check they are up to date (CI); change nothing")
    t.set_defaults(run=cmd_templates)
    c = sub.add_parser("check", help="check a folder of tables against the codebook")
    c.add_argument("folder", nargs="?", default=str(CORPUS / "merged"),
                   help="corpus/merged (default) or corpus/out/<source_id>")
    c.set_defaults(run=cmd_check)
    sub.add_parser("ping", help="one tiny live call per literature service"
                   ).set_defaults(run=cmd_ping)
    args = p.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
