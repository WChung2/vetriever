#!/usr/bin/env bash
# Everything CI checks. Run it before every push:   ./scripts/check.sh
# CI (.github/workflows/ci.yml) runs this same script, so passing here means passing there.
set -euo pipefail
cd "$(dirname "$0")/.."

step() { printf '\n== %s\n' "$1"; }
fail() { printf '\nFAILED: %s\n' "$1" >&2; exit 1; }

step "1/5 packages match uv.lock"
uv sync --locked

step "2/5 lint (ruff)"
uv run --no-sync ruff check .

step "3/5 tests (network blocked)"
uv run --no-sync pytest -q

step "4/5 templates/ and codebook.md match codebook.csv"
uv run --no-sync vet templates --check

step "5/5 nothing secret or private in the files git would publish"
# the files a 'git add -A' would include: tracked + new, minus .gitignore'd
files=$(git ls-files --cached --others --exclude-standard)
private=$(printf '%s\n' "$files" | grep -E '(^|/)\.env$|^corpus/' || true)
[ -z "$private" ] || fail "private files would be published (keep them git-ignored):
$private"
google_keys=$(git grep --untracked -lE 'AIza[0-9A-Za-z_-]{35}' || true)
[ -z "$google_keys" ] || fail "something that looks like a Google API key is in: $google_keys"
if [ -f .env ]; then          # your own key values must not appear in any published file
  while IFS='=' read -r name value; do
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    [ "${#value}" -ge 8 ] || continue
    leaks=$(git grep --untracked -lF -- "$value" || true)
    [ -z "$leaks" ] || fail "the value of $name from .env appears in: $leaks"
  done < <(grep -E '^[A-Z_]+=' .env)
fi
echo "ok"

printf '\nAll checks passed.\n'
