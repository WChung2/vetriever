"""Gemini, called directly through google-genai, behind a hard spending cap.

`LLM.call(step, prompt, schema)` returns the parsed JSON answer. Before each call the
ledger reserves the worst case (the prompt plus the maximum output, at the settings prices)
and refuses if that could pass the cap; afterwards the real usage is written to
corpus/spend.csv. The ledger lives on disk, so the cap holds across runs.
Every call is saved to corpus/calls/<doc>/ unless save_calls is off.

`FakeGemini` answers without a network call, so tests cost nothing.
"""
import csv
import json
import threading
import time
from datetime import date, datetime
from pathlib import Path

from .settings import CORPUS, env, settings

RETRY_CODES = {429, 500, 503}


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    """Money spent so far (corpus/spend.csv) plus money reserved by calls in flight."""
    COLUMNS = ("time", "step", "doc", "model", "input_tokens", "output_tokens",
               "thoughts_tokens", "usd")

    def __init__(self, path: Path, cap: float):
        self.path, self.cap = path, cap
        self.lock = threading.Lock()
        self.reserved = 0.0
        self.spent = 0.0
        if path.exists():
            with open(path, newline="", encoding="utf-8") as fh:
                self.spent = sum(float(r["usd"]) for r in csv.DictReader(fh))

    def reserve(self, usd: float):
        with self.lock:
            if self.spent + self.reserved + usd > self.cap:
                raise BudgetExceeded(
                    f"spending cap ${self.cap:.2f} reached: ${self.spent:.4f} spent, "
                    f"${self.reserved:.4f} in flight, next call up to ${usd:.4f}")
            self.reserved += usd

    def settle(self, reserved: float, row: dict):
        with self.lock:
            self.reserved -= reserved
            self.spent += row["usd"]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            new = not self.path.exists()
            with open(self.path, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=self.COLUMNS, lineterminator="\n")
                if new:
                    w.writeheader()
                w.writerow(row)


class Gemini:
    """The real model. Needs a paid-tier GEMINI_API_KEY in .env."""

    def __init__(self):
        from google import genai
        key = env("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set in .env (paid tier only)")
        self.client = genai.Client(api_key=key)

    def generate(self, model, system, prompt, schema, thinking, max_output, step=""):
        from google.genai import types
        r = self.client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system or None, response_mime_type="application/json",
                response_json_schema=schema, max_output_tokens=max_output,
                thinking_config=types.ThinkingConfig(thinking_level=thinking)))
        u = r.usage_metadata
        return r.text or "", {"input": u.prompt_token_count or 0,
                              "output": u.candidates_token_count or 0,
                              "thoughts": u.thoughts_token_count or 0}


def _empty_answer(schema: dict):
    """The smallest answer that fits a JSON schema: what FakeGemini says by default."""
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        return {k: _empty_answer(v) for k, v in schema.get("properties", {}).items()
                if k in schema.get("required", [])}
    return {"array": [], "string": "", "integer": 0, "number": 0, "boolean": False}.get(t)


class FakeGemini:
    """Deterministic stand-in. `responder(step, prompt, schema)` returns the answer object;
    the default returns the smallest valid answer. Steps add realistic responders (including
    deliberately wrong quotes) as they are built, so the checks are exercised for free."""

    def __init__(self, responder=None):
        self.responder = responder or (lambda step, prompt, schema: _empty_answer(schema))
        self.calls = []

    def generate(self, model, system, prompt, schema, thinking, max_output, step=""):
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        answer = json.dumps(self.responder(step, prompt, schema))
        return answer, {"input": len(system + prompt) // 4, "output": len(answer) // 4, "thoughts": 0}


class LLM:
    def __init__(self, backend=None, cap: float | None = None, save_calls: bool | None = None,
                 corpus: Path = CORPUS):
        cfg = settings()
        self.cfg = cfg
        self.backend = backend or Gemini()
        self.fake = isinstance(self.backend, FakeGemini)
        spend = cfg["spending"]
        self.save = spend["save_calls"] if save_calls is None else save_calls
        self.corpus = corpus
        # fake calls are counted in their own ledger so they never touch real spending
        self.ledger = Ledger(corpus / ("spend_fake.csv" if self.fake else "spend.csv"),
                             spend["cap_usd"] if cap is None else cap)

    def price(self, model: str) -> dict:
        prices = self.cfg["prices"]
        if not self.fake and date.today().isoformat() > prices["valid_until"]:
            raise BudgetExceeded(f"prices in settings.toml expired on {prices['valid_until']}; "
                                 "re-check them before paid calls")
        if model not in prices:
            raise BudgetExceeded(f"no price for {model} in settings.toml")
        return prices[model]

    def call(self, step: str, prompt: str, schema: dict, system: str = "", doc: str = "_"):
        model = self.cfg["models"][step]
        thinking = self.cfg["thinking"][step]
        max_out = self.cfg["spending"]["max_output_tokens"]
        p = self.price(model)
        # ~3 characters per token over-estimates English text: errs towards stopping early
        worst = (len(system + prompt) / 3 * p["input"] + max_out * p["output"]) / 1e6
        self.ledger.reserve(worst)
        try:
            for attempt in range(3):
                try:
                    text, usage = self.backend.generate(model, system, prompt, schema, thinking,
                                                       max_out, step=step)
                    break
                except Exception as e:
                    if getattr(e, "code", None) not in RETRY_CODES or attempt == 2:
                        raise
                    time.sleep(2 ** (attempt + 2))
        except Exception:
            self.ledger.settle(worst, self._row(step, doc, model, {}, 0.0))
            raise
        usd = (usage["input"] * p["input"] + (usage["output"] + usage["thoughts"]) * p["output"]) / 1e6
        self.ledger.settle(worst, self._row(step, doc, model, usage, usd))
        if self.save:
            self._save(step, doc, model, system, prompt, schema, text, usage, usd)
        return json.loads(text)

    @staticmethod
    def _row(step, doc, model, usage, usd):
        return {"time": datetime.now().isoformat(timespec="seconds"), "step": step, "doc": doc,
                "model": model, "input_tokens": usage.get("input", 0),
                "output_tokens": usage.get("output", 0),
                "thoughts_tokens": usage.get("thoughts", 0), "usd": round(usd, 6)}

    def _save(self, step, doc, model, system, prompt, schema, text, usage, usd):
        folder = self.corpus / "calls" / doc
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        (folder / f"{stamp}_{step}.json").write_text(json.dumps(
            {"step": step, "model": model, "fake": self.fake, "system": system, "prompt": prompt,
             "schema": schema, "answer": text, "usage": usage, "usd": usd},
            ensure_ascii=False, indent=1), encoding="utf-8")
