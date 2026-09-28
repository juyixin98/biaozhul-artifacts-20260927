#!/usr/bin/env bash
# Local verification: unit/integration tests + live HTTP smoke + log leak grep.
# Synthetic data only; nothing here contacts external services.
set -u
cd "$(dirname "$0")/.."

PY=.venv/bin/python
export LOGSAFE_DB="${LOGSAFE_DB:-./var/verify.sqlite3}"
export LOGSAFE_AUDIT_KEY="local-synthetic-audit-key"
rm -f "$LOGSAFE_DB" "$LOGSAFE_DB-wal" "$LOGSAFE_DB-shm"

echo "== 1/4 pytest (assertions on concrete results + failure categories) =="
$PY -m pytest -q
PYTEST_RC=$?

echo
echo "== 2/4 start local uvicorn (127.0.0.1:8088) =="
$PY run_server.py > var/server.log 2>&1 &
SERVER_PID=$!
cleanup() { kill "$SERVER_PID" 2>/dev/null || true; wait "$SERVER_PID" 2>/dev/null || true; }
trap cleanup EXIT

for _ in $(seq 1 50); do
  if curl -sf http://127.0.0.1:8088/health >/dev/null 2>&1; then break; fi
  sleep 0.2
done

echo "== 3/4 live HTTP smoke checks =="
$PY scripts/smoke_live.py
SMOKE_RC=$?

echo
echo "== 4/4 service log must not contain any synthetic secret =="
LEAK=0
for secret in \
  "alice.synth@example.test" \
  "sk_test_SYNTH1234567890abcdef" \
  "$($PY -c 'from tests.synth_fixtures import SYNTH_BANK_CARD as s; print(s)')" \
  "$($PY -c 'from tests.synth_fixtures import SYNTH_CN_ID as s; print(s)')"; do
  if grep -F -q "$secret" var/server.log; then
    echo "  LEAK detected in var/server.log: ${secret:0:6}..."
    LEAK=1
  fi
done
[ "$LEAK" -eq 0 ] && echo "  no secrets found in service log"

echo
echo "SUMMARY: pytest_rc=$PYTEST_RC smoke_rc=$SMOKE_RC log_leak=$LEAK"
if [ "$PYTEST_RC" -eq 0 ] && [ "$SMOKE_RC" -eq 0 ] && [ "$LEAK" -eq 0 ]; then
  echo "VERIFY: PASS"
  exit 0
fi
echo "VERIFY: FAIL"
exit 1
