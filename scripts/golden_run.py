"""Gemini answers the golden questions from one route. Public, openly licensed papers only.

    uv run python scripts/golden_run.py <pdf|pypdf|mineru> [run]

    pdf     Gemini gets the PDF itself
    pypdf   Gemini gets the text pypdf + vet/pdftext.py made, as the pipeline sends it today
    mineru  Gemini gets the text MinerU standard made on this Mac

Every route gets the same model, settings and questions; only the paper's form differs. Paid
through the spending ledger and stopped at LIMIT_USD. Prints counts only.
"""
import concurrent.futures as cf
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from pypdf import PdfReader

from vet.llm import Ledger
from vet.settings import CORPUS, env, settings

ROOT = Path("corpus/golden")
MODEL = "gemini-3.8-flash"
LIMIT_USD = 0.60                  # for one route and one run
MAX_OUT = 8192
PROMPT = """Answer each question from this paper's tables only. Each question names a table, a row
and a column. A " › " separates levels: "Blood › PCV" is the row PCV under the subheading Blood,
and "Treated › Day 7" is the Day 7 column under Treated.

For each question, give what is printed in that cell.
- Copy it character for character, with its symbols, units and footnote marks. Do not correct,
  round or calculate anything.
- If a cell has two or more lines, join them with " / ".
- If the cell is empty, or you cannot tell what is printed in it, answer exactly NOT REPORTED.
  Do not guess, and do not take a value from a neighbouring cell.

Answer every question, using its id.

Questions:
"""
SCHEMA = {"type": "object", "required": ["answers"], "properties": {"answers": {
    "type": "array", "items": {"type": "object", "required": ["id", "value"], "properties": {
        "id": {"type": "string"}, "value": {"type": "string"}}}}}}


def papers() -> dict[str, list[dict]]:
    """Golden facts by paper, for papers listed as CC BY or CC BY-NC. Nothing else is ever sent."""
    allowed = {r["pmcid"] for r in csv.DictReader((ROOT / "papers.csv").open())
               if r["licence"] in ("by", "by-nc")}
    out = {}
    for fact in json.loads((ROOT / "golden.json").read_text()).values():
        if fact["paper"] in allowed:
            out.setdefault(fact["paper"], []).append(fact)
    return out


def paper_part(route: str, pmc: str):
    if route == "pdf":
        return types.Part.from_bytes(data=(ROOT / "pdf" / f"{pmc}.pdf").read_bytes(),
                                     mime_type="application/pdf")
    text = (ROOT / "text" / route / f"{pmc}.md").read_text()
    return f'<document source_id="{pmc}">\n{text}\n</document>'


def ask(client, contents, config):
    for attempt in range(6):
        try:
            return client.models.generate_content(model=MODEL, contents=contents, config=config)
        except Exception as e:
            temporary = getattr(e, "code", None) in (429, 500, 503) or "Timeout" in type(e).__name__
            if not temporary or attempt == 5:
                raise
            time.sleep(min(2 ** (attempt + 2), 60))


def worst_case(route: str, pmc: str, price: dict) -> float:
    pages = len(PdfReader(ROOT / "pdf" / f"{pmc}.pdf").pages)
    tokens = pages * 1500 if route == "pdf" else \
        len((ROOT / "text" / route / f"{pmc}.md").read_text()) // 2
    return (tokens * price["input"] + MAX_OUT * price["output"]) / 1e6


def answer(client, route: str, pmc: str, facts: list[dict], ledger: Ledger, price: dict) -> dict:
    codes = {f"q{i}": f["id"] for i, f in enumerate(facts, 1)}   # a fact's id gives its position
    questions = [{"id": code, **{k: f[k] for k in ("table", "row", "column")}}
                 for code, f in zip(codes, facts)]
    worst = worst_case(route, pmc, price)
    ledger.reserve(worst)
    used, usd = (0, 0, 0), 0.0
    try:
        r = ask(client, [paper_part(route, pmc), PROMPT + json.dumps(questions, ensure_ascii=False)],
                types.GenerateContentConfig(
                    max_output_tokens=MAX_OUT, response_mime_type="application/json",
                    response_schema=SCHEMA,
                    thinking_config=types.ThinkingConfig(thinking_level="low")))
        u = r.usage_metadata
        used = (u.prompt_token_count or 0, u.candidates_token_count or 0,
                u.thoughts_token_count or 0)
        usd = (used[0] * price["input"] + (used[1] + used[2]) * price["output"]) / 1e6
        reason = str(r.candidates[0].finish_reason).rsplit(".", 1)[-1] if r.candidates else "NONE"
        got = json.loads(r.text or "{}").get("answers", [])
        return {"answers": {codes[a["id"]]: a["value"] for a in got if a["id"] in codes},
                "finish": reason,
                "tokens": used, "usd": round(usd, 6)}
    finally:
        ledger.settle(worst, {"time": datetime.now().isoformat(timespec="seconds"),
                              "step": "golden", "doc": pmc, "model": MODEL,
                              "input_tokens": used[0], "output_tokens": used[1],
                              "thoughts_tokens": used[2], "usd": round(usd, 6)})


def main():
    route, run = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "1")
    if route not in ("pdf", "pypdf", "mineru"):
        raise SystemExit(f"unknown route: {route}")
    out = ROOT / "answers" / f"{route}_{run}"
    out.mkdir(parents=True, exist_ok=True)
    cfg = settings()
    assert datetime.now().date().isoformat() <= cfg["prices"]["valid_until"]
    probe = Ledger(CORPUS / "spend.csv", cfg["spending"]["cap_usd"])
    ledger = Ledger(CORPUS / "spend.csv", min(cfg["spending"]["cap_usd"], probe.spent + LIMIT_USD))
    client = genai.Client(api_key=env("GEMINI_API_KEY"),
                          http_options=types.HttpOptions(timeout=300_000))

    def one(pmc: str, facts: list[dict]):
        if (out / f"{pmc}.json").exists():
            return None
        result = answer(client, route, pmc, facts, ledger, cfg["prices"][MODEL])
        (out / f"{pmc}.json").write_text(json.dumps(result, ensure_ascii=False, indent=0))
        return result

    done = failed = 0
    with cf.ThreadPoolExecutor(3) as pool:
        jobs = [pool.submit(one, k, v) for k, v in sorted(papers().items())]
        for job in cf.as_completed(jobs):
            try:
                done += job.result() is not None
            except Exception as e:  # noqa: BLE001 - one paper failing must not stop the rest
                failed += 1
                print("failed:", type(e).__name__, str(e)[:160], flush=True)
    print(f"{route} run {run}: answered {done}, failed {failed}, spent now ${ledger.spent:.4f}")


if __name__ == "__main__":
    main()
