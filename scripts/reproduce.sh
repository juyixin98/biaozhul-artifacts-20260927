#!/usr/bin/env bash
# Reproduce every verifiable result: build, vet, unit/fault/store/adapter
# tests, and the independent black-box acceptance verifier (real binary, real
# HTTP, real SQLite, real process restart). Outputs are retained under
# results/ for later review.
set -euo pipefail
# Resolve repository root (this script lives in scripts/).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p results

echo "== go version =="
go version

echo "== go build ./... =="
go build ./...

echo "== go vet ./... =="
go vet ./...

echo "== go test (fast suite: unit, fault injection, store, HTTP adapter) =="
go test ./... -count=1 -skip TestBlackBoxAcceptance 2>&1 | tee results/unit-tests.txt

echo
echo "== independent black-box acceptance verifier =="
echo "   (builds and launches the real server; includes a real restart)"
rm -rf results/accept-work
mkdir -p results/accept-work
set +e
go run ./cmd/acceptance -workdir results/accept-work 2>results/accept-server.log | tee results/acceptance-report.txt
rc=${PIPESTATUS[0]}
set -e

echo
if [[ $rc -eq 0 ]]; then
  echo "ALL CHECKS PASSED (report: results/acceptance-report.txt)"
else
  echo "ACCEPTANCE FAILED (see results/acceptance-report.txt and results/accept-server.log)"
fi
exit $rc
