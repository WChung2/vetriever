"""Gemini, called directly through google-genai, behind a hard spending cap.

`LLM.call(step, prompt, schema)` returns the parsed JSON answer. Before each call the
ledger reserves the worst case (the prompt plus the maximum output, at the settings prices)
and refuses if that could pass the cap; afterwards the real usage is written to
corpus/spend.csv. The ledger lives on disk, so the cap holds across runs.
Every call, with the model's summary of its thinking, is saved to corpus/calls/<doc>/
unless save_calls is off.

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


CALL_TIMEOUT_MS = 600_000   # a silent connection must not hang a run for ever
CACHE_TTL = "1800s"      # a held document outlives its calls by far; it is deleted after them


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
        from google.genai import types
        key = env("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set in .env (paid tier only)")
        self.client = genai.Client(http_options=types.HttpOptions(timeout=CALL_TIMEOUT_MS), api_key=key)

    def open_cache(self, model, system, text):
        """Hold a document at Google for the calls that follow: its name and size in tokens."""
        from google.genai import types
        c = self.client.caches.create(model=model, config=types.CreateCachedContentConfig(
            system_instruction=system or None, contents=[text], ttl=CACHE_TTL))
        return c.name, c.usage_metadata.total_token_count or 0

    def close_cache(self, name):
        self.client.caches.delete(name=name)

    def generate(self, model, system, prompt, schema, thinking, max_output, step="", cache=""):
        """The answer, the token counts, and the model's summary of its thinking. cache: a held
        document, which then carries the system prompt too."""
        from google.genai import types
        # "none" sends no thinking setting, for models that refuse a thinking level
        # (Gemini 2.5 Flash-Lite thinks only when given a thinking budget)
        thinking_config = None if thinking == "none" else types.ThinkingConfig(
            thinking_level=thinking, include_thoughts=True)
        r = self.client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=None if cache else system or None,
                cached_content=cache or None, response_mime_type="application/json",
                response_json_schema=schema, max_output_tokens=max_output,
                thinking_config=thinking_config))
        u = r.usage_metadata
        parts = (r.candidates[0].content.parts or []) if r.candidates and r.candidates[0].content else []
        text = "".join(p.text or "" for p in parts if not p.thought)
        thoughts = "\n".join(p.text or "" for p in parts if p.thought)
        return text, {"input": u.prompt_token_count or 0,
                      "output": u.candidates_token_count or 0,
                      "thoughts": u.thoughts_token_count or 0,
                      "cached": getattr(u, "cached_content_token_count", 0) or 0}, thoughts


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
        self.calls, self.held, self.closed = [], [], []

    def open_cache(self, model, system, text):
        self.held.append(text)
        return f"cache-{len(self.held)}", len(system + text) // 4

    def close_cache(self, name):
        self.closed.append(name)

    def generate(self, model, system, prompt, schema, thinking, max_output, step="", cache=""):
        self.calls.append({"model": model, "system": system, "prompt": prompt, "cache": cache})
        answer = json.dumps(self.responder(step, prompt, schema))
        held = len(self.held[-1]) // 4 if cache else 0
        return answer, {"input": len(system + prompt) // 4 + held, "output": len(answer) // 4,
                        "thoughts": 0, "cached": held}, ""


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
        self.held = {}          # documents held at Google: name -> (tokens, time opened)

    def price(self, model: str) -> dict:
        prices = self.cfg["prices"]
        if model not in prices:
            raise BudgetExceeded(f"no price for {model} in settings.toml")
        # a model whose price changes before the others carries its own valid_until
        until = prices[model].get("valid_until", prices["valid_until"])
        if not self.fake and date.today().isoformat() > until:
            raise BudgetExceeded(f"the price of {model} in settings.toml expired on {until}; "
                                 "re-check it before paid calls")
        return prices[model]

    def open_cache(self, step: str, system: str, text: str, doc: str = "_") -> str:
        """Send a document once, for all its calls. Empty if it can't be held (too short, or the
        model refuses): the calls then send it themselves."""
        model = self.cfg["models"][step]
        p = self.price(model)
        worst = len(system + text) / 3 * p["input"] / 1e6
        self.ledger.reserve(worst)
        try:
            name, tokens = self.backend.open_cache(model, system, text)
        except Exception:  # noqa: BLE001 - no cache only means each call sends the paper itself
            self.ledger.settle(worst, self._row(step, doc, model, {}, 0.0))
            return ""
        self.ledger.settle(worst, self._row(step, doc, model, {"input": tokens},
                                            tokens * p["input"] / 1e6))
        self.held[name] = (tokens, time.time())
        return name

    def close_cache(self, name: str, step: str, doc: str = "_"):
        """Delete a held document and pay for the time it was held."""
        model = self.cfg["models"][step]
        tokens, opened = self.held.pop(name)
        self.backend.close_cache(name)
        hours = (time.time() - opened) / 3600
        usd = tokens * self.price(model).get("cache_hour", 0) * hours / 1e6
        self.ledger.reserve(usd)
        self.ledger.settle(usd, self._row(step, doc, model, {}, usd))

    def call(self, step: str, prompt: str, schema: dict, system: str = "", doc: str = "_",
             cache: str = ""):
        model = self.cfg["models"][step]
        thinking = self.cfg["thinking"][step]
        spend = self.cfg["spending"]
        max_out = spend.get("max_output_tokens_by_step", {}).get(step, spend["max_output_tokens"])
        p = self.price(model)
        # ~3 characters per token over-estimates English text: errs towards stopping early
        worst = (len(system + prompt) / 3 * p["input"] + max_out * p["output"]) / 1e6
        held = {"cache": cache} if cache else {}
        self.ledger.reserve(worst)
        try:
            for attempt in range(3):
                try:
                    text, usage, thoughts = self.backend.generate(
                        model, system, prompt, schema, thinking, max_out, step=step, **held)
                    break
                except Exception as e:
                    temporary = (getattr(e, "code", None) in RETRY_CODES
                                 or "Timeout" in type(e).__name__)
                    if not temporary or attempt == 2:
                        raise
                    time.sleep(2 ** (attempt + 2))
        except Exception:
            self.ledger.settle(worst, self._row(step, doc, model, {}, 0.0))
            raise
        cached = usage.get("cached", 0)          # within input, at the price for cached tokens
        usd = ((usage["input"] - cached) * p["input"] + cached * p.get("cached", p["input"])
               + (usage["output"] + usage["thoughts"]) * p["output"]) / 1e6
        self.ledger.settle(worst, self._row(step, doc, model, usage, usd))
        if self.save:
            self._save(step, doc, model, system, prompt, schema, text, usage, usd, thoughts)
        return json.loads(text)

    @staticmethod
    def _row(step, doc, model, usage, usd):
        return {"time": datetime.now().isoformat(timespec="seconds"), "step": step, "doc": doc,
                "model": model, "input_tokens": usage.get("input", 0),
                "output_tokens": usage.get("output", 0),
                "thoughts_tokens": usage.get("thoughts", 0), "usd": round(usd, 6)}

    def _save(self, step, doc, model, system, prompt, schema, text, usage, usd, thoughts):
        folder = self.corpus / "calls" / doc
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        (folder / f"{stamp}_{step}.json").write_text(json.dumps(
            {"step": step, "model": model, "fake": self.fake, "system": system, "prompt": prompt,
             "schema": schema, "thoughts": thoughts, "answer": text, "usage": usage, "usd": usd},
            ensure_ascii=False, indent=1), encoding="utf-8")
