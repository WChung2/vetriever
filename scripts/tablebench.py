"""Run the table reader benchmark's free steps. Prints counts only, never paper text.

    uv run python scripts/tablebench.py read-pypdf
    uv run python scripts/tablebench.py packets <reader>...
    uv run python scripts/tablebench.py score

Everything it reads and writes is in corpus/tablebench/ (git-ignored): sealed.csv lists the
papers, pdf/ their files, out/<reader>/<PMCID>.md what each reader gave, packets/ what the judges
see, answers.json who is who, and marks/ what the judges said.
"""
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from vet import pdftext
from vet import tablebench as tb

ROOT = Path("corpus/tablebench")
BATCH_CHARS = 60_000


def papers() -> list[dict]:
    return list(csv.DictReader((ROOT / "sealed.csv").open()))


def read_pypdf():
    out = ROOT / "out" / "pypdf_repair"
    out.mkdir(parents=True, exist_ok=True)
    for p in papers():
        text, _ = pdftext.read((ROOT / "pdf" / f"{p['pmcid']}.pdf").read_bytes())
        (out / f"{p['pmcid']}.md").write_text(text)
    print("read", len(papers()), "papers")


def packets(readers: list[str]):
    shown, sealed = [], {}
    for p in papers():
        tables = tb.read_tables(Path("corpus") / p["xml"])
        outputs = {}
        for name in readers:
            f = ROOT / "out" / name / f"{p['pmcid']}.md"
            outputs[name] = f.read_text() if f.exists() else None
        s, a = tb.packets(p["pmcid"], tables, outputs)
        shown += s
        sealed.update(a)
    (ROOT / "answers.json").write_text(json.dumps(sealed))
    folder = ROOT / "packets"
    folder.mkdir(exist_ok=True)
    for old in folder.glob("*.json"):
        old.unlink()
    batch, size, n = [], 0, 0
    for packet in [*shown, None]:
        length = len(json.dumps(packet, ensure_ascii=False)) if packet else 0
        if batch and (packet is None or size + length > BATCH_CHARS):
            n += 1
            (folder / f"batch_{n:02}.json").write_text(json.dumps(
                {"instructions": tb.JUDGE, "tables": batch}, ensure_ascii=False, indent=0))
            batch, size = [], 0
        if packet:
            batch.append(packet)
            size += length
    print("tables", len(shown), "batches", n,
          "chars", sum(f.stat().st_size for f in folder.glob("*.json")),
          "shown per table", Counter(len(p["entries"]) for p in shown).most_common(4))


def score():
    sealed = json.loads((ROOT / "answers.json").read_text())
    marks = {}
    for f in sorted((ROOT / "marks").glob("*.json")):
        marks.update(json.loads(f.read_text()))
    result = tb.score(sealed, marks)
    print(tb.checks_pass(result)[1], dict(result["planted"]))
    for name, c in sorted(result["totals"].items()):
        total = sum(c.values())
        print(f"{name:<16} cells {total:>5}  correct {100 * c['correct'] / total:5.1f}%  "
              f"wrong {100 * c['wrong'] / total:4.1f}%  missing {100 * c['missing'] / total:4.1f}%")
    return result


if __name__ == "__main__":
    command, *rest = sys.argv[1:] or ["help"]
    {"read-pypdf": read_pypdf, "packets": lambda: packets(rest), "score": score}.get(
        command, lambda: print(__doc__))()
