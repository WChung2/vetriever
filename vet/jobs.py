"""The slow or paid steps the screens start, as work for chat.Runner.

Each opens its own database connection, in its own thread, in autocommit: a job may spend
minutes on the network, and an open transaction would lock the vet's clicks out of the
database. A job's model calls run under the search's budget: the ledger refuses any call that
could pass it. Running out is not an error: the round stays open and can be carried on.
"""
from collections.abc import Callable
from pathlib import Path

from . import budget, chat, plan, review
from . import connectors as c
from . import search as s
from .llm import LLM, BudgetExceeded


class Tools:
    """The database and folders a job works on; `backend` is a stand-in model in tests."""

    def __init__(self, db: Path, corpus: Path, backend=None):
        self.db, self.corpus, self.backend = Path(db), Path(corpus), backend

    def llm(self) -> LLM:
        return LLM(self.backend, corpus=self.corpus)

    def connect(self):
        con = s.connect(self.db)
        con.isolation_level = None
        return con


def plan_first(tools: Tools, name: str, words: str) -> Callable:
    def work(say):
        say("reading your question and checking its terms in PubMed")
        plan.run_round(name, tools.llm(), c.Http(), words=words)
    return work


def plan_next(tools: Tools, name: str, answers: dict) -> Callable:
    def work(say):
        say("redrawing the search with your answers")
        plan.run_round(name, tools.llm(), c.Http(), answers=answers)
    return work


def agree(tools: Tools, name: str, answers: dict) -> Callable:
    def work(say):
        say("counting the records this search finds (free)")
        try:
            plan.agree(name, c.Http(), answers)
        except ValueError as e:
            chat.add(tools.db, name, "event", "stopped", {"text": str(e)})
    return work


def _budgeted(tools: Tools, name: str, step: Callable) -> Callable:
    """A search step under the topic's budget."""
    def work(say):
        con = tools.connect()
        try:
            llm = tools.llm()
            llm.ledger.cap = min(llm.ledger.cap, llm.ledger.spent
                                 + budget.left(con, tools.corpus, name))
            step(name, llm, c.Http(), con, lambda text: say(text.strip()))
        except BudgetExceeded as e:
            chat.add(tools.db, name, "event", "stopped",
                     {"text": f"Stopped before passing the budget: {e}. Nothing is lost: what "
                              "was screened is saved, and you can carry on."})
        finally:
            con.close()
    return work


def carry_on(tools: Tools, name: str) -> Callable:
    return _budgeted(tools, name, review.carry_on)


def widen(tools: Tools, name: str) -> Callable:
    return _budgeted(tools, name, review.widen)


def ask_next(tools: Tools, name: str) -> Callable:
    return _budgeted(tools, name, review.ask_next)
