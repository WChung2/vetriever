#!/usr/bin/env bash
# Live smoke test: the real literature services and publishers, on fixed IMHA papers whose
# answers we know (tests/test_smoke.py). Not part of CI: it needs the internet, the keys in
# .env and, for the subscription papers, the EPFL network or VPN. It writes only to a
# temporary folder and costs nothing. Run it after changing a connector or vet/fetch.py.
#
#   scripts/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== services"
uv run vet ping
echo "== pipeline steps on known papers"
VET_LIVE=1 uv run pytest tests/test_smoke.py -v -p no:cacheprovider
