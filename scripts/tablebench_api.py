"""Gemini and Gemma read the benchmark's tables. Public, openly licensed papers only.

    uv run --with pypdfium2 --with pillow python scripts/tablebench_api.py <model> <practice|sealed> [PMCID...]

    model: gemini-3.8-flash, gemma-4-31b-it or gemma-4-26b-a4b-it

Gemini gets the whole PDF, paid through the spending ledger and stopped at LIMIT_USD. Gemma has no
paid tier, so Google may use what it is sent: it gets page images (its documentation covers images,
not PDFs), and only papers listed in corpus/tablebench as CC BY or CC BY-NC. Prints counts only.
"""
import concurrent.futures as cf
import csv
import io
import sys
import time
from datetime import datetime
from pathlib import Path

import pypdfium2 as pdfium
from google import genai
from google.genai import types
from pypdf import PdfReader

from vet.llm import Ledger
from vet.settings import CORPUS, env, settings

ROOT = Path("corpus/tablebench")
LIMIT_USD = 0.80
MAX_OUT = 16384
DPI = 200
RULES = """- Write each table as a Markdown table, one table row per line, with its caption on the line
above and its footnotes on the lines below.
- Copy every number, symbol, unit and word character for character. Do not correct, round,
calculate, reorder or fill in anything.
- A blank cell stays blank. Keep footnote marks. If a value cannot be read, write [unreadable] in
that cell.
- A subheading row inside a table stays its own row."""
WHOLE = f"Copy every data table in this paper, exactly as printed.\n{RULES}\n" \
        "Write nothing except the tables."
PAGE = f"Copy every data table on this page, exactly as printed.\n{RULES}\n" \
       "Write nothing except the tables. If the page has no data table, write NONE."


def allowed(group: str) -> dict[str, Path]:
    """Papers this run may send, by PMCID. A paper with any other licence is never listed."""
    folder = ROOT / group
    rows = csv.DictReader((ROOT / f"{group}.csv").open())
    pdfs = ROOT / "pdf" if group == "sealed" else folder / "pdf"
    return {r["pmcid"]: pdfs / f"{r['pmcid']}.pdf" for r in rows
            if r["licence"] in ("by", "by-nc")}


def call(client, model: str, contents, config):
    for attempt in range(6):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as e:
            temporary = getattr(e, "code", None) in (429, 500, 503) or "Timeout" in type(e).__name__
            if not temporary or attempt == 5:
                raise
            time.sleep(min(2 ** (attempt + 2), 60))


def reason(r) -> str:
    return str(r.candidates[0].finish_reason).rsplit(".", 1)[-1] if r.candidates else "NONE"


def tokens(r) -> tuple[int, int, int]:
    u = r.usage_metadata
    return (u.prompt_token_count or 0, u.candidates_token_count or 0, u.thoughts_token_count or 0)


def read_gemini(client, model, pdf: Path, ledger: Ledger, price: dict) -> tuple[str, list]:
    pages = len(PdfReader(pdf).pages)
    worst = (pages * 1500 * price["input"] + MAX_OUT * price["output"]) / 1e6
    ledger.reserve(worst)
    used, usd = (0, 0, 0), 0.0
    try:
        r = call(client, model, [types.Part.from_bytes(data=pdf.read_bytes(),
                                                       mime_type="application/pdf"), WHOLE],
                 types.GenerateContentConfig(
                     max_output_tokens=MAX_OUT,
                     thinking_config=types.ThinkingConfig(thinking_level="low")))
        used = tokens(r)
        usd = (used[0] * price["input"] + (used[1] + used[2]) * price["output"]) / 1e6
        return r.text or "", [pages, *used, round(usd, 5), reason(r)]
    finally:
        ledger.settle(worst, {"time": datetime.now().isoformat(timespec="seconds"),
                              "step": "tablebench", "doc": pdf.stem, "model": model,
                              "input_tokens": used[0], "output_tokens": used[1],
                              "thoughts_tokens": used[2], "usd": round(usd, 6)})


def read_gemma(client, model, pdf: Path) -> tuple[str, list]:
    document = pdfium.PdfDocument(pdf)
    config = types.GenerateContentConfig(
        max_output_tokens=MAX_OUT, media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH)
    parts, total, reasons = [], [0, 0, 0], set()
    for page in document:
        buffer = io.BytesIO()
        page.render(scale=DPI / 72).to_pil().save(buffer, "PNG")
        try:
            r = call(client, model, [types.Part.from_bytes(data=buffer.getvalue(),
                                                           mime_type="image/png"), PAGE], config)
        except Exception as e:  # noqa: BLE001 - a lost page is logged, the paper goes on
            reasons.add(f"LOST-{type(e).__name__}")
            continue
        total = [a + b for a, b in zip(total, tokens(r))]
        reasons.add(reason(r))
        text = (r.text or "").strip()
        if text and text != "NONE":
            parts.append(text)
    return "\n\n".join(parts), [len(document), *total, 0, "+".join(sorted(reasons))]


def main():
    model, group, *only = sys.argv[1:]
    papers = allowed(group)
    if only:
        unknown = set(only) - set(papers)
        if unknown:
            raise SystemExit(f"not on the allowed list: {sorted(unknown)}")
        papers = {k: v for k, v in papers.items() if k in only}
    gemini = model.startswith("gemini")
    out = ROOT / ("out" if group == "sealed" else f"{group}/out") / model
    out.mkdir(parents=True, exist_ok=True)
    cfg = settings()
    probe = Ledger(CORPUS / "spend.csv", cfg["spending"]["cap_usd"])
    ledger = Ledger(CORPUS / "spend.csv", min(cfg["spending"]["cap_usd"], probe.spent + LIMIT_USD))
    if gemini:
        assert datetime.now().date().isoformat() <= cfg["prices"]["valid_until"]
    client = genai.Client(api_key=env("GEMINI_API_KEY"),
                          http_options=types.HttpOptions(timeout=300_000))

    def one(pmc: str, pdf: Path):
        if (out / f"{pmc}.md").exists():
            return None
        start = time.time()
        text, row = (read_gemini(client, model, pdf, ledger, cfg["prices"][model]) if gemini
                     else read_gemma(client, model, pdf))
        (out / f"{pmc}.md").write_text(text)
        with (out / "usage.csv").open("a") as f:
            f.write(",".join(map(str, [pmc, round(time.time() - start, 1), *row])) + "\n")
        return row

    done = failed = 0
    with cf.ThreadPoolExecutor(3) as pool:
        for job in cf.as_completed([pool.submit(one, k, v) for k, v in sorted(papers.items())]):
            try:
                done += job.result() is not None
            except Exception as e:  # noqa: BLE001 - one paper failing must not stop the rest
                failed += 1
                print("failed:", type(e).__name__, str(e)[:160], flush=True)
    print(f"{model}: read {done}, failed {failed}, spent now ${ledger.spent:.4f}")


if __name__ == "__main__":
    main()
