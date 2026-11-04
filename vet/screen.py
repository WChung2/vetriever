"""Screening (layer 2): is a search hit on topic, so that its full text is worth reading?

The system prompt is prompts/screen.md filled from the topic's [screen] section, which the
Planner writes from the vet's words and the query expansion; the template itself names no
species or condition. The model reads each record (title, abstract, PubMed subject terms),
writes its reasoning, and answers two checks with a quote each: condition, and animals
(which also says whether they are the topic's species). It also writes a one-sentence
summary for the vet's list. Code decides from the answers (`decide`): a record is dropped only on a clear "no"
whose quote is in the record; anything unclear, unanswered or unbacked is kept.
"""
import json
from concurrent.futures import ThreadPoolExecutor

from .settings import ROOT
from .validate import _flat

BATCH = 10
SLOTS = ("species", "condition", "also_called", "related", "treatments")
SENT = ("key", "year", "title", "abstract", "subject_terms", "publication_types")


def _check(answers: list[str]) -> dict:
    return {"type": "object", "required": ["answer", "quote"],
            "properties": {"answer": {"type": "string", "enum": answers},
                           "quote": {"type": "string"}}}


SCHEMA = {"type": "object", "required": ["records"], "properties": {"records": {
    "type": "array", "items": {
        "type": "object",
        "required": ["key", "reasoning", "condition", "animals", "kind", "design", "summary"],
        "properties": {
            "key": {"type": "string"},
            "reasoning": {"type": "string"},
            "condition": _check(["yes", "related", "no", "unclear"]),
            "animals": _check(["patients", "healthy_treated", "survey", "other_species", "none",
                               "unclear"]),
            "kind": {"type": "string", "enum": ["study", "review"]},
            "design": {"type": "string", "enum": ["randomised_trial", "prospective",
                                                  "retrospective", "case_series", "single_case",
                                                  "other", "unclear"]},
            "summary": {"type": "string"}}}}}}

# the check whose "no" each off-topic rule rests on
RULE_CHECK = {"O1": "animals", "O2": "condition", "O3": "animals"}


def system_prompt(name: str, topic: dict) -> str:
    """prompts/screen.md filled from topics/<name>.toml; refuses a topic with gaps."""
    s = topic.get("screen", {})
    missing = [k for k in SLOTS if not s.get(k)] + ([] if topic.get("question") else ["question"])
    if missing:
        raise ValueError(f"topics/{name}.toml [screen] lacks {missing}: the Planner writes "
                         "these from the vet's words and the query expansion")
    values = {k: "; ".join(v) if isinstance(v, list) else v for k, v in s.items() if k in SLOTS}
    return (ROOT / "prompts" / "screen.md").read_text("utf-8").format(
        question=" ".join(topic["question"].split()), **values)


def decide(a: dict, text: str) -> tuple[str, str, list[str]]:
    """keep | lead | off_topic, the off-topic rule, and flags, from one record's answers.
    `text` is what the model saw (title, abstract, subject terms): a drop's quote must be in it."""
    condition, animals = a["condition"]["answer"], a["animals"]["answer"]
    if animals == "other_species":
        rule = "O1"
    elif animals == "healthy_treated":    # healthy animals given a treatment: no condition needed
        rule = ""
    elif condition == "no":
        rule = "O2"
    elif animals == "none" and a["kind"] != "review":   # a review has no animals of its own
        rule = "O3"
    else:
        rule = ""
    flags = [f for f, on in (("related_condition", condition == "related"),
                             ("research_animals", animals == "healthy_treated"),
                             ("survey", animals == "survey"),
                             ("single_case", a["design"] == "single_case")) if on]
    if rule:
        quote = a[RULE_CHECK[rule]]["quote"]
        if not quote.strip() or _flat(quote) not in _flat(text):
            return "keep", "", flags + ["unbacked_drop"]
        return "off_topic", rule, flags
    return ("lead" if a["kind"] == "review" else "keep"), "", flags


def screen(name: str, topic: dict, records: list[dict], llm, batch: int = BATCH,
           workers: int = 4) -> list[dict]:
    """One result per record, in order. A record is a dict with the keys in SENT (the
    service's own names: subject_terms = MeSH headings, publication_types)."""
    system = system_prompt(name, topic)
    batches = [records[i:i + batch] for i in range(0, len(records), batch)]

    def run(part):
        prompt = "Records, one JSON object per line:\n" + "\n".join(
            json.dumps({k: r.get(k, "") for k in SENT}, ensure_ascii=False) for r in part)
        for attempt in range(2):        # an answer cut off (e.g. by long thinking): once more
            try:
                got = llm.call("screen", prompt, SCHEMA, system=system, doc=f"screen_{name}")
                return {a.get("key"): a for a in got.get("records", [])}
            except json.JSONDecodeError:
                pass
        return {}                       # its records are kept, flagged not_answered

    with ThreadPoolExecutor(workers) as pool:
        answers = {}
        for got in pool.map(run, batches):
            answers |= got
    out = []
    for r in records:
        a = answers.get(r["key"])
        flags = [] if r.get("abstract", "").strip() else ["title_only"]
        if a is None:
            out.append({"key": r["key"], "decision": "keep", "rule": "",
                        "flags": flags + ["not_answered"], "answers": {}})
            continue
        text = " ".join(r.get(k, "") for k in ("title", "abstract", "subject_terms"))
        decision, rule, more = decide(a, text)
        out.append({"key": r["key"], "decision": decision, "rule": rule,
                    "flags": flags + more, "answers": a})
    return out
