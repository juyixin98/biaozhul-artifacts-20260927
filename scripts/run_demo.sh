#!/usr/bin/env bash
# Run the in-process end-to-end demo (isolated scenario kernels + replay).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m localffg.cli demo --db data/demo/root.db
