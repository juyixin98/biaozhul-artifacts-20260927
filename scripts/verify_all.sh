#!/usr/bin/env bash
# End-to-end verification:
#   1. go vet + go test -race (packet, storage, state machine, raw-UDP fixtures)
#   2. build the pinned binary
#   3. start a loopback-only test server (fresh SQLite, fake clock)
#   4. run the independent Python oracle over raw UDP + HTTP diagnostics
#   5. always stop the server; propagate the worst exit status
#
# Nothing here touches a non-loopback interface.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p build/testrun

STATUS=0
fail() { echo "VERIFY-FAIL: $*" >&2; STATUS=1; }

echo "== [1/5] go version / dependency check =="
go version
go mod verify

echo "== [2/5] go vet =="
if ! go vet ./...; then fail "go vet"; fi

echo "== [3/5] go test -race (count=1) =="
if ! go test -race -count=1 -v ./... 2>&1 | tee build/testrun/go-test.log | grep -E '^(ok|FAIL|---)'; then
  fail "go tests reported failure"
fi
if grep -q '^FAIL' build/testrun/go-test.log; then fail "go test FAIL lines present"; fi

echo "== [4/5] build pinned binary =="
if ! go build -trimpath -o build/testrun/dhcpd ./cmd/dhcpd; then
  fail "go build"
fi

echo "== [5/5] independent Python reference oracle =="
"$ROOT/scripts/stop_test_server.sh" >/dev/null 2>&1 || true
if ! "$ROOT/scripts/start_test_server.sh"; then
  fail "test server did not start"
else
  RUN_ID="$(cat build/testrun/run.id 2>/dev/null || echo unknown)"
  # Give the listeners a brief settle window beyond the health gate.
  sleep 0.2
  set +e
  python3 fixtures/dhcp_oracle.py \
    --udp 127.0.0.1:10067 \
    --http http://127.0.0.1:18080 \
    --run-id "$RUN_ID" \
    --pool-lo 127.50.0.2 \
    --contention 4 \
    --report build/testrun/oracle_report.json
  ORACLE_RC=$?
  set -e
  if [[ $ORACLE_RC -ne 0 ]]; then
    fail "python oracle exit=$ORACLE_RC (see build/testrun/oracle_report.json)"
  fi
fi
"$ROOT/scripts/stop_test_server.sh" >/dev/null 2>&1 || true

echo
if [[ $STATUS -eq 0 ]]; then
  echo "VERIFY-OK: all Go tests and independent oracle assertions passed"
  echo "artifacts:"
  echo "  build/testrun/go-test.log"
  echo "  build/testrun/oracle_report.json"
  echo "  build/testrun/server-${RUN_ID:-run}.log"
else
  echo "VERIFY-FAIL: one or more checks failed" >&2
fi
exit $STATUS
