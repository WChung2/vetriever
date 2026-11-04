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
        root = folder.parent.parent if source_id else CORPUS     # <root>/out/<id>, <root>/docs
        tables, headers = load_folder(folder, CORPUS)
        reports, name = root / "reports", source_id or folder.name
        scope = f"document {source_id}" if source_id else f"folder {folder.name}"
    else:
        from .duplicates import possible_duplicates
        from .store import Store
        store = Store(args.db)
        tables, headers, source_id, root = store.tables(), None, None, args.db.parent
        reports, name, scope = args.db.parent / "reports", "database", str(args.db)
        review = possible_duplicates(tables)
        write_report(review, reports / "duplicates_to_review.csv")
        print(f"to review: {len(review)} possible duplicates; list in "
              f"{reports / 'duplicates_to_review.csv'}")
        for check, n in Counter(i.check for i in review).most_common():
            print(f"  {n:5d}  {check}")
    issues = validate(tables, Codebook(), source_id=source_id, headers=headers, root=root)
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


def _topic_ids(args) -> list[str]:
    """PMIDs or DOIs given, from --from, or the topic's approved records (--topic)."""
    ids = list(args.ids) + (Path(args.from_file).read_text("utf-8").split() if args.from_file
                            else [])
    if getattr(args, "topic", None):
        from .approve import approved
        from .search import connect
        ids += [r["pmid"] or r["doi"] for r in approved(args.topic, connect(args.db))
                if r["pmid"] or r["doi"]]
    if not ids:
        sys.exit("give PMIDs or DOIs, --from a file with one per line, or --topic")
    return ids


def cmd_fetch(args):
    from .fetch import fetch
    ids = _topic_ids(args)
    folder = args.db.parent
    try:
        papers = fetch(ids, folder, db=args.db)
    except ValueError as e:
        sys.exit(str(e))
    got = Counter(p.route for p in papers if p.route)
    print(f"full text for {sum(got.values())} of {len(papers)} papers in {folder / 'docs'}: "
          + ", ".join(f"{route} {n}" for route, n in got.most_common()))
    warned = sum(bool(p.text_warning) for p in papers)
    if warned:
        print(f"{warned} PDFs may have wrong symbols (<, >, =): check values with them by eye "
              "(text_warning in docs/index.csv)")
    left = len(papers) - sum(got.values())
    if left and args.topic:
        from .approve import without_text
        from .search import connect
        from .settings import settings
        n = settings()["pipeline"]["hand_download"]
        best = without_text(args.topic, connect(args.db))
        path = folder / "reports" / f"{args.topic}_download_by_hand.md"
        path.write_text(f"# {args.topic}: {len(best)} approved papers without a full text\n\n"
                        f"Worth downloading by hand (the top {n}; put the PDF in {folder / 'docs'} "
                        "named <PMID>.pdf):\n\n" + "\n".join(
                            f"{i}. {r['title']} ({r['year']}, {r['design']}): "
                            f"https://pubmed.ncbi.nlm.nih.gov/{r['pmid']}/" for i, r in
                            enumerate(best[:n], 1)) + "\n\nThe rest are extracted from their "
                        "abstracts, flagged abstract_only.\n", "utf-8")
        print(f"{left} without a full text; the {min(n, len(best))} worth downloading by hand: {path}")
    elif left:
        print(f"{left} to download by hand: {folder / 'reports' / 'to_download.csv'}")


def cmd_who(args):
    from .who import institutions, who
    ids = _topic_ids(args)
    try:
        missing = who(ids, args.db)
    except ValueError as e:
        sys.exit(str(e))
    print(f"authors of {len(ids) - len(missing)} of {len(ids)} papers stored in {args.db}")
    if missing:
        print(f"  not in OpenAlex: {', '.join(missing[:20])}")
    print(f"  {'papers':>6s} {'authors':>7s}  institution (countries)")
    for r in institutions(args.db, ids)[:15]:
        print(f"  {r['papers']:6d} {r['authors']:7d}  {r['root_name']} ({r['countries']})")


def _spent(llm, before: float) -> str:
    return f"Gemini cost ${llm.ledger.spent - before:.2f} (total ${llm.ledger.spent:.2f} of the ${llm.ledger.cap:.2f} cap)"


def cmd_plan(args):
    from . import connectors as c
    from . import plan
    from .llm import LLM
    if args.start:
        print(f"wrote {plan.start(args.topic)}; next: uv run vet search {args.topic}")
        return
    llm = LLM()
    before = llm.ledger.spent
    try:
        state = plan.run_round(args.topic, llm, c.Http(), words=" ".join(args.words),
                               answers_file=args.answers)
    except ValueError as e:
        sys.exit(str(e))
    n = len(state["rounds"])
    print(f"round {n}: read {plan.folder(args.topic) / f'round{n}.md'}")
    print(f"  then answer in {plan.folder(args.topic) / f'answers{n}.toml'} and run "
          f"'uv run vet plan {args.topic} --answers <that file>', or '--start'")
    print("  " + _spent(llm, before))


def cmd_search(args):
    from . import review
    from .llm import LLM
    from .search import connect
    llm = LLM()
    before = llm.ledger.spent
    try:
        out = review.run(args.topic, llm, db=connect(args.db))
    except ValueError as e:
        sys.exit(str(e))
    print(f"{out['kept']} records kept after {len(out['rounds'])} rounds; "
          f"next: uv run vet list {args.topic}")
    print(_spent(llm, before))


def cmd_list(args):
    from . import approve
    from .search import connect
    db = connect(args.db)
    if args.approve_all:
        print(f"approved {approve.approve(args.topic, db)} kept records")
    kept, details = approve.write_list(args.topic, db, args.db.parent / "reports")
    print(f"the list: {kept}\nreasoning, flags and dropped records: {details}")


def cmd_extract(args):
    """PMIDs, DOIs or source_ids; a paper with no full text is read from its abstract."""
    from .extract import run_extract
    from .llm import LLM, BudgetExceeded
    from .search import connect
    from .store import document_key
    db = connect(args.db)
    keys, abstracts = [], {}
    for k in args.ids:
        r = db.execute("SELECT * FROM records WHERE key = ? OR pmid = ? OR (doi != '' AND "
                       "lower(doi) = lower(?))", (k, k, k)).fetchone()
        if r is None:
            keys.append(k)                       # already a source_id
            continue
        sid = document_key({"doi": r["doi"], "pmid": r["pmid"], "source_id": ""})
        keys.append(sid)
        abstracts[sid] = {"title": r["title"], "abstract": r["abstract"], "pmid": r["pmid"],
                          "doi": r["doi"]}
    llm = LLM()
    before = llm.ledger.spent
    try:
        reports = run_extract(keys, args.topic, llm, args.db, abstracts=abstracts)
    except (ValueError, BudgetExceeded) as e:
        sys.exit(str(e))
    print(f"{sum(bool(r.study) for r in reports)} of {len(reports)} documents stored in "
          f"{args.db}; problems in its extraction_log table")
    print(_spent(llm, before))

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


def _port_free(host: str, port: int) -> bool:
    import socket
    with socket.socket() as s:
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def cmd_ui(args):
    """The screens, on this Mac only: there is deliberately no option to listen elsewhere."""
    import threading
    import webbrowser

    import uvicorn

    from .ui.app import HOST, create_app
    if not _port_free(HOST, args.port):     # else the browser would open another program's page
        sys.exit(f"Port {args.port} is used by another program on this Mac. "
                 f"Try: vet ui --port {args.port + 1}")
    url = f"http://{HOST}:{args.port}/"
    print(f"Vetriever: {url}  (only this Mac can open it; Ctrl-C stops it)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, (url,)).start()
    # the spending log and the Planner's saved rounds sit beside the database (corpus/)
    uvicorn.run(create_app(args.db, args.db.parent), host=HOST, port=args.port, log_level="warning",
                server_header=False, proxy_headers=False)


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
    f = sub.add_parser("fetch", help="download and check full texts into docs/ beside the "
                       "database; list the rest to download by hand")
    f.add_argument("ids", nargs="*", help="PMIDs or DOIs")
    f.add_argument("--from", dest="from_file", help="a file with one PMID or DOI per line")
    f.add_argument("--topic", help="fetch the topic's approved records; rank the rest for the vet")
    f.set_defaults(run=cmd_fetch)
    w = sub.add_parser("who", help="step 7: authors and institutions from OpenAlex (free)")
    w.add_argument("ids", nargs="*", help="PMIDs or DOIs")
    w.add_argument("--from", dest="from_file", help="a file with one PMID or DOI per line")
    w.add_argument("--topic", help="the topic's approved records")
    w.set_defaults(run=cmd_who)
    x = sub.add_parser("extract", help="step 9: read documents into the database (paid); a "
                       "paper with no full text is read from its abstract")
    x.add_argument("topic", help="a topic in topics/ (needs its [screen] species and condition)")
    x.add_argument("ids", nargs="+", help="PMIDs, DOIs or source_ids")
    x.set_defaults(run=cmd_extract)
    e = sub.add_parser("export", help="write the database out as CSV tables")
    e.add_argument("folder", nargs="?", default=str(CORPUS / "merged"))
    e.set_defaults(run=cmd_export)
    sub.add_parser("ping", help="one tiny live call per literature service"
                   ).set_defaults(run=cmd_ping)
    pl = sub.add_parser("plan", help="step 1: the vet's words into topics/<topic>.toml (paid)")
    pl.add_argument("topic")
    pl.add_argument("words", nargs="*", help="the vet's words (first round only)")
    pl.add_argument("--answers", type=Path, help="the vet's answers to the last round")
    pl.add_argument("--start", action="store_true", help="write the topic file from the last round")
    pl.set_defaults(run=cmd_plan)
    se = sub.add_parser("search", help="steps 2-5: search, screen and review until nothing new (paid)")
    se.add_argument("topic")
    se.set_defaults(run=cmd_search)
    ls = sub.add_parser("list", help="step 6: the kept list for the vet; --approve-all approves it")
    ls.add_argument("topic")
    ls.add_argument("--approve-all", action="store_true")
    ls.set_defaults(run=cmd_list)
    ui = sub.add_parser("ui", help="the screens in your browser, on this Mac only (free)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true", help="don't open the browser")
    ui.set_defaults(run=cmd_ui)
    args = p.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
