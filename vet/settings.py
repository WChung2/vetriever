"""Project paths, settings.toml (models, spending cap, prices) and secrets from .env
(API keys, contact email)."""
import os
import tomllib
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"          # docs/, out/, merged/, reports/ and the database
DB = CORPUS / "vetriever.db"


@cache
def settings() -> dict:
    return tomllib.loads((ROOT / "settings.toml").read_text("utf-8"))


@cache
def _dotenv() -> dict:
    path = ROOT / ".env"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text("utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        if sep and not name.startswith("#"):
            out[name.strip()] = value.strip().strip("'\"")
    return out


def env(name: str) -> str:
    """A secret from the environment or .env; empty when not set. Never print these."""
    return os.environ.get(name) or _dotenv().get(name, "")
