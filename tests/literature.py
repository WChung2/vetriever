"""A tiny fake literature (offline, free): records, and a PubMed that evaluates the queries
code builds ("x"[Mesh], word[tiab], AND / OR / NOT, parentheses, 123[pmid])."""
import re

from vet import connectors as c

TOKEN = re.compile(r'\(|\)|\bAND\b|\bOR\b|\bNOT\b|"[^"]+"\[\w+\]|[^\s()"]+\[\w+\]')


class Literature:
    def __init__(self, records: list[c.Record], mesh: dict[str, list[str]],
                 references: dict[str, list[str]] | None = None):
        self.records = {r.pmid: r for r in records}
        self.mesh = mesh                       # heading -> entry terms
        self.references = references or {}    # pmid -> pmids it cites
        self.queries: list[str] = []

    def _match(self, r: c.Record, token: str) -> bool:
        term, field = re.fullmatch(r'"?([^"\[]+)"?\[(\w+)\]', token).groups()
        if field.lower() == "mesh":
            return term.lower() in [m.lower() for m in r.mesh.split("|")]
        if field.lower() == "pmid":
            return r.pmid == term
        text = f"{r.title} {r.abstract}".lower()
        word = term.lower()
        if word.endswith("*"):
            return re.search(r"\b" + re.escape(word[:-1]), text) is not None
        return re.search(r"\b" + re.escape(word) + r"\b", text) is not None

    def _eval(self, q: str, r: c.Record) -> bool:
        py = []
        for tok in TOKEN.findall(q):
            if tok in "()":
                py.append(tok)
            elif tok == "AND":
                py.append("and")
            elif tok == "OR":
                py.append("or")
            elif tok == "NOT":
                py.append("and not")
            else:
                py.append(str(self._match(r, tok)))
        return eval(" ".join(py))   # only True, False, and, or, not and parentheses

    # the connectors' signatures
    def pubmed_search(self, http, query, limit=100, sort=""):
        self.queries.append(query)
        ids = [p for p, r in sorted(self.records.items()) if self._eval(query, r)]
        return len(ids), ids[:limit]

    def pubmed_records(self, http, pmids):
        return [self.records[p] for p in pmids if p in self.records]

    def mesh_heading(self, http, name):
        for h, entries in self.mesh.items():
            if h.lower() == name.lower():
                return {"name": h, "entry_terms": entries}
        return None

    def openalex_works(self, http, limit=100, **params):
        flt = params.get("filter", "")
        if flt.startswith("pmid:"):
            p = flt[5:]
            return (1, [c.Record("openalex", pmid=p, openalex_id=f"W{p}")]) if p in self.records else (0, [])
        if flt.startswith("cited_by:W"):
            cited = self.references.get(flt[10:], [])
            return len(cited), [c.Record("openalex", pmid=p, openalex_id=f"W{p}") for p in cited]
        return 0, []

    def europepmc_search(self, http, query, limit=100):
        return 0, []

    def pubmed_similar(self, http, pmid, limit=20):
        return []

    def install(self, monkeypatch):
        for name in ("pubmed_search", "pubmed_records", "mesh_heading", "openalex_works",
                     "europepmc_search", "pubmed_similar"):
            monkeypatch.setattr(c, name, getattr(self, name))
        monkeypatch.setattr(c, "log_search", lambda *a: None)
        return self


def rec(pmid: str, title: str, mesh: str = "", abstract: str = "", types: str = "Journal Article"):
    return c.Record("pubmed", pmid=pmid, title=title, abstract=abstract or title, mesh=mesh,
                    pub_types=types, year="2020")
