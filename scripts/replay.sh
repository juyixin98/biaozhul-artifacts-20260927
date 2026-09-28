#!/usr/bin/env bash
# Replays every request fixture against a running admissiond and prints the
# verdicts. Usage: scripts/replay.sh [base URL]
set -euo pipefail
BASE="${1:-http://127.0.0.1:8080}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

for f in "$DIR"/testdata/requests/*.json; do
  echo "==> $(basename "$f")"
  curl -sS -X POST "$BASE/admission" \
    -H 'Content-Type: application/json' \
    --data-binary "@$f" \
  | python3 -c '
import sys, json
r = json.load(sys.stdin)["response"]
print("  uid=%s allowed=%-5s replayed=%-5s category=%-15s plugin=%s" % (
    r["uid"], str(r["allowed"]), str(r.get("replayed", False)),
    r.get("failureCategory", "-"), r.get("failedPlugin", "-")))
print("  final: replicas=%s cpu=%s mem=%s fp=%s" % (
    r["finalSummary"].get("replicas"), r["finalSummary"].get("cpu"),
    r["finalSummary"].get("memory"), r["finalSummary"].get("fingerprint")))
if r.get("denyReason"): print("  deny:  %s" % r["denyReason"])
if r.get("message") and not r["allowed"]: print("  msg:   %s" % r["message"])
'
done

echo
echo "==> pending retries: $BASE/retry/queue"
curl -sS "$BASE/retry/queue"; echo
