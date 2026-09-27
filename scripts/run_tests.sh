#!/usr/bin/env bash
# Full reproducible test run with a correlated run id.
#
# Produces:
#   runs/test-results/<run_id>/pytest.log     full pytest output
#   runs/test-results/<run_id>/summary.json   pass/fail, versions, summary
#   runs/truth-differential.jsonl             per-case verdicts (written by
#                                             the differential test itself)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate

RUN_ID="${SEARCHDSL_RUN_ID:-test-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="runs/test-results/$RUN_ID"
mkdir -p "$OUT"
export SEARCHDSL_RUN_ID="$RUN_ID"

echo "run_id=$RUN_ID"
python - "$OUT" "$RUN_ID" <<'PY'
import json, sys
import searchdsl
from searchdsl import DSL_SPEC_VERSION, INDEX_SCHEMA_VERSION
out_dir, run_id = sys.argv[1], sys.argv[2]
json.dump(
    {
        "run_id": run_id,
        "package_version": searchdsl.__version__,
        "dsl_version": DSL_SPEC_VERSION,
        "index_schema_version": INDEX_SCHEMA_VERSION,
    },
    open(f"{out_dir}/context.json", "w"),
    indent=2,
)
print("package:", searchdsl.__version__, "dsl:", DSL_SPEC_VERSION,
      "index:", INDEX_SCHEMA_VERSION)
PY

set +e
python -m pytest tests/ -v 2>&1 | tee "$OUT/pytest.log"
STATUS=${PIPESTATUS[0]}
set -e

python - "$OUT" "$STATUS" "$RUN_ID" <<'PY'
import json, sys
out_dir, status, run_id = sys.argv[1], int(sys.argv[2]), sys.argv[3]
import re as _re
log_lines = open(f"{out_dir}/pytest.log").read().strip().splitlines()
summary_line = ""
for l in reversed(log_lines):
    if _re.search(r"\d+ (?:passed|failed)", l):
        summary_line = l.strip()
        break
summary = {
    "run_id": run_id,
    "verdict": "pass" if status == 0 else "fail",
    "exit_code": status,
    "pytest_summary": summary_line,
}
json.dump(summary, open(f"{out_dir}/summary.json", "w"), indent=2)
print("verdict:", summary["verdict"], "-", summary_line)
PY

if [ -f runs/truth-differential.jsonl ]; then
  cp runs/truth-differential.jsonl "$OUT/truth-differential.jsonl"
fi

exit "$STATUS"
