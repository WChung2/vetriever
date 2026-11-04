"""Who and where: the authors of the approved papers and their institutions, from OpenAlex.
Free, and it works for paywalled papers too, since it needs no full text.

Each author of each paper is a row of the authors table in the database, one row per
institution the paper gives them. The institution is rolled up into its root through
OpenAlex's lineage (the institution and all its parents), so a vet school or department
counts for its university; each root is looked up once. `institutions` ranks the roots by
papers and authors.
"""
import sqlite3
from pathlib import Path

from . import connectors as c
from .fetch import paper_id
from .settings import env
from .store import norm_doi

INSTITUTIONS = c.OPENALEX.removesuffix("works") + "institutions/"
BATCH = 50              # OpenAlex takes up to 50 values in one filter (a|b|c)
COLUMNS = ["paper", "author_no", "author_position", "author_name", "orcid", "author_id",
           "institution_name", "institution_id", "country_code", "root_name", "root_id",
           "work_id"]


def who(ids: list[str], db: Path, http: c.Http | None = None) -> list[str]:
    """Store the authors of each paper (PMID or DOI) in the authors table, replacing what was
    stored for it before (an author without an institution is one row with it empty); returns
    the ids OpenAlex does not have."""
    http = http or c.Http()
    asked = [(raw, *paper_id(raw)) for raw in ids]
    works = {}
    for kind in ("pmid", "doi"):
        works |= _works(http, kind, list(dict.fromkeys(v for _, k, v in asked if k == kind)))
    con = _connect(db)
    missing = []
    try:
        roots = {i: (name, [i]) for i, name in con.execute(      # known from earlier runs
            "SELECT DISTINCT root_id, root_name FROM authors WHERE root_id != ''")}
        for raw, kind, v in asked:
            w = works.get((kind, v))
            if w is None:
                missing.append(raw)
                continue
            rows = _rows(http, v, w, roots)
            with con:
                con.execute("DELETE FROM authors WHERE paper = ?", (v,))
                con.executemany(f"INSERT OR REPLACE INTO authors ({', '.join(COLUMNS)}) "
                                f"VALUES ({', '.join('?' for _ in COLUMNS)})",
                                [[r[k] for k in COLUMNS] for r in rows])
    finally:
        con.close()
    return missing


def institutions(db: Path, ids: list[str] | None = None) -> list[dict]:
    """Root institutions, most papers first, then most authors: root_id, root_name,
    countries (|-joined country codes), papers, authors. Only these papers when ids is given."""
    con = _connect(db)
    try:
        return rank(con, ids)
    finally:
        con.close()


def rank(con: sqlite3.Connection, ids: list[str] | None = None,
         lead_only: bool = False) -> list[dict]:
    """`institutions` on an open connection (the screens pass a read-only one). lead_only
    counts only first and last authors, who usually lead a paper; middle authors often help
    with one part. Each institution also lists its people's names, most papers first."""
    rows = con.execute("SELECT paper, work_id, author_id, author_name, country_code, "
                       "root_id, root_name FROM authors WHERE root_id != ''"
                       + (" AND author_position IN ('first', 'last')" if lead_only else "")
                       ).fetchall()
    wanted = {paper_id(i)[1] for i in ids} if ids is not None else None
    groups = {}
    for paper, work, author, name, country, root, root_name in rows:
        if wanted is not None and paper not in wanted:
            continue
        g = groups.setdefault(root, {"root_id": root, "root_name": root_name,
                                     "countries": set(), "papers": set(), "authors": set(),
                                     "by": {}})
        g["countries"].add(country)
        g["papers"].add(work or paper)          # a paper asked by PMID and by DOI counts once
        g["authors"].add(author or name)
        g["by"].setdefault(name, set()).add(work or paper)
    out = [{k: v for k, v in g.items() if k != "by"}
           | {"countries": "|".join(sorted(g["countries"] - {""})), "papers": len(g["papers"]),
              "authors": len(g["authors"]),
              "names": [n for n, _ in sorted(g["by"].items(), key=lambda x: (-len(x[1]), x[0]))]}
           for g in groups.values()]
    return sorted(out, key=lambda g: (-g["papers"], -g["authors"], g["root_name"]))


def _params(**more) -> dict:
    if env("OPENALEX_API_KEY"):
        more["api_key"] = env("OPENALEX_API_KEY")
    if env("CONTACT_EMAIL"):
        more["mailto"] = env("CONTACT_EMAIL")
    return more


def _short(url: str | None) -> str:
    """https://openalex.org/I123 -> I123; https://orcid.org/0000-… -> 0000-…"""
    return (url or "").rsplit("/", 1)[-1]


def _works(http: c.Http, kind: str, values: list[str]) -> dict[tuple, dict]:
    """OpenAlex's work for each PMID or DOI it has, by (kind, value)."""
    out = {}
    for k in range(0, len(values), BATCH):
        # per-page above BATCH: room for a paper OpenAlex holds twice (the first is used)
        res = http.get(c.OPENALEX, _params(filter=f"{kind}:{'|'.join(values[k:k + BATCH])}",
                                           select="id,doi,ids,authorships",
                                           **{"per-page": 200})).json()
        for w in res["results"]:
            v = _short((w.get("ids") or {}).get("pmid")) if kind == "pmid" else \
                norm_doi(w.get("doi") or "")
            out.setdefault((kind, v), w)
    return out


def _rows(http: c.Http, paper: str, w: dict, roots: dict) -> list[dict]:
    out = []
    for n, a in enumerate(w.get("authorships") or [], 1):
        person = a.get("author") or {}
        author = {"paper": paper, "author_no": n,
                  "author_position": a.get("author_position") or "",
                  "author_name": person.get("display_name") or a.get("raw_author_name") or "",
                  "orcid": _short(person.get("orcid")), "author_id": _short(person.get("id")),
                  "work_id": _short(w.get("id"))}
        for inst in a.get("institutions") or [{}]:
            root, root_name = _root(http, inst, roots) if inst.get("id") else ("", "")
            out.append(author | {"institution_name": inst.get("display_name") or "",
                                 "institution_id": _short(inst.get("id")),
                                 "country_code": inst.get("country_code") or "",
                                 "root_name": root_name, "root_id": root})
    return out


def _root(http: c.Http, inst: dict, roots: dict) -> tuple[str, str]:
    """(id, name) of the top of the institution's lineage: the one with no parent. OpenAlex
    does not say which end of the lineage is the top, so the parents' own lineages are looked
    up, each once (`roots` keeps them: id -> (name, lineage))."""
    me = _short(inst["id"])
    parents = [i for i in map(_short, inst.get("lineage") or []) if i != me]
    if not parents:
        return me, inst.get("display_name") or ""
    for i in parents:
        if i not in roots:
            got = http.get(INSTITUTIONS + i, _params()).json()
            roots[i] = (got.get("display_name") or "",
                        [_short(x) for x in got.get("lineage") or [i]])
        if roots[i][1] == [i]:
            return i, roots[i][0]
    return parents[-1], roots[parents[-1]][0]      # no clear top (a loop in the data)


def _connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    with con:
        con.execute("""CREATE TABLE IF NOT EXISTS authors (
            paper TEXT NOT NULL,                -- the PMID or DOI it was asked by
            author_no INTEGER NOT NULL,         -- 1, 2, … in the author list
            author_position TEXT NOT NULL DEFAULT '',   -- first | middle | last
            author_name TEXT NOT NULL DEFAULT '',
            orcid TEXT NOT NULL DEFAULT '',
            author_id TEXT NOT NULL DEFAULT '',         -- OpenAlex A…
            institution_name TEXT NOT NULL DEFAULT '',
            institution_id TEXT NOT NULL DEFAULT '',    -- OpenAlex I…
            country_code TEXT NOT NULL DEFAULT '',
            root_name TEXT NOT NULL DEFAULT '',
            root_id TEXT NOT NULL DEFAULT '',           -- the top of the lineage
            work_id TEXT NOT NULL DEFAULT '',           -- OpenAlex W…
            PRIMARY KEY (paper, author_no, institution_id))""")
    return con
