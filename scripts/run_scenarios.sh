#!/usr/bin/env bash
# Run all bundled offline replay scenarios. Each run prints the diagnostic
# trace, the embedded assertions and the SQLite journal-rebuild check.
set -euo pipefail
cd "$(dirname "$0")/.."

for s in testdata/scenarios/*.json; do
  echo "############################################################"
  echo "# $s"
  echo "############################################################"
  go run ./cmd/igmpv2timer replay "$s"
  echo
done

echo "== independent tests (core cross-checked against the separate oracle) =="
go test -v ./testind/...
