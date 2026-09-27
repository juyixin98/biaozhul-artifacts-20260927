#!/usr/bin/env bash
# Run every bundled synthetic fixture offline, save full JSON reports and
# decision logs, and print a summary table. No network neighbors are dialed.
set -u
cd "$(dirname "$0")/.."

export GOPROXY=off
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="testdata/out/$stamp"
mkdir -p "$out" bin

# Build once: `go run` collapses a child's non-zero exit code to 1, which
# would hide the distinction between input failure (2) and engine failure (1).
if ! go build -o bin/pvreplay ./cmd/pvreplay; then
  echo "build failed"
  exit 1
fi
PV="$PWD/bin/pvreplay"

printf '%-22s %-14s %-22s %-6s %s\n' FIXTURE STATUS REASON STEPS EXIT
fail=0
for f in testdata/fixtures/*.json; do
  name="$(basename "$f" .json)"
  "$PV" -log "$out/$name.log" "$f" > "$out/$name.report.json" 2> "$out/$name.stderr"
  code=$?
  status="$(grep -o '"status": *"[^"]*"' "$out/$name.report.json" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
  reason="$(grep -o '"reason": *"[^"]*"' "$out/$name.report.json" | head -1 | sed 's/.*"\([^"]*\)"$/\1/')"
  steps="$(grep -o '"steps": *[0-9]*' "$out/$name.report.json" | head -1 | grep -o '[0-9]*')"
  printf '%-22s %-14s %-22s %-6s %s\n' "$name" "${status:-LOAD_ERROR}" "${reason:-}" "${steps:-}" "$code"
  # invalid_input fixtures fail validation before any run exists (exit 1,
  # no report); hard engine failures (state conflict, queue cap) exit 1 but
  # still emit a partial report; clean runs exit 0.
  if [ "$name" = "invalid_input" ]; then
    [ "$code" -eq 2 ] || { echo "  expected exit 2 for invalid_input"; fail=1; }
  elif [ "$name" = "unknown_withdraw" ] || [ "$name" = "queue_cap" ]; then
    [ "$code" -eq 1 ] || { echo "  expected exit 1 for $name"; fail=1; }
    [ -s "$out/$name.report.json" ] || { echo "  expected partial report for $name"; fail=1; }
  else
    [ "$code" -eq 0 ] || { echo "  expected exit 0 for $name, got $code"; cat "$out/$name.stderr"; fail=1; }
  fi
done

echo
echo "artifacts: $out"
exit "$fail"
