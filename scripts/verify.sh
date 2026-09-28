# Local verification entry point.
# Runs the full suite, then prints the latest replay summary and fails the
# script if any test failed or errored.
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "virtualenv missing; run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 2
fi

"$PY" -m pytest "$@"

latest=$(ls -dt test-runs/run-*/ 2>/dev/null | head -1 || true)
if [ -z "$latest" ]; then
  echo "no test-run directory produced" >&2
  exit 1
fi
echo
echo "Replay log: ${latest%/}"
"$PY" - "$latest/summary.json" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"run_number={d['run_number']} passed={d['passed']} "
      f"failed={d['failed']} skipped={d['skipped']}")
if d["failed"]:
    print("FAILED TESTS:")
    for t in d["failed_tests"]:
        print("  -", t)
    sys.exit(1)
PYEOF
