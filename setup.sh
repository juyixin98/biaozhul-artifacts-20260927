#!/usr/bin/env bash
# Create a virtualenv and install locked dependencies.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
if [[ -f requirements.lock.txt ]]; then
  python -m pip install -r requirements.lock.txt
else
  python -m pip install -r requirements.txt
fi
echo
echo "Installed. Next steps:"
echo "  source .venv/bin/activate"
echo "  pytest                       # full suite (golden vs eth_abi + security + api)"
echo "  python tools/selfcheck.py    # offline stdlib-only self-check (no deps)"
echo "  bash run.sh serve            # HTTP API"
echo "  bash run.sh replay           # offline synthetic replay"
