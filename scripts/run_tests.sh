#!/usr/bin/env bash
# Runs the complete evidence suite:
#   1. internal unit tests (geometry enumerated pointwise, config, analyzer,
#      store, replay)
#   2. independent black-box tests (test/ module with its own oracle):
#      exhaustive reduced-space comparisons, rule swaps, default-deny,
#      safe-deletion invariance, parse failure categories, explainability.
# Also runs go vet and builds the server binary.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export GOTOOLCHAIN="${GOTOOLCHAIN:-local}"

echo "== go vet =="
go vet ./...
( cd test && go vet ./... )

echo
echo "== build =="
go build ./...

echo
echo "== unit tests (implementation modules) =="
go test -count=1 ./...

echo
echo "== independent evidence suite (own oracle, black-box HTTP) =="
( cd test && go test -count=1 -v ./... )

echo
echo "ALL TESTS PASSED"
