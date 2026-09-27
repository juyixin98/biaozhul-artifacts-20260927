#!/usr/bin/env bash
# Reproducible review script.
#
#   scripts/verify.sh            # full: clippy + tests + exhaustive CLI verify of every fixture
#   scripts/verify.sh --quick    # skip the release build and clippy
#
# Exit codes:
#   0  every step passed
#   1  build/lint/test failure
#   4  one or more differential verifications are unsound
#   5  a verification was reported NOT RUN (domain cap), i.e. incomplete evidence
#
# The script never treats "not run" as "passed": any NOT RUN result fails it.
set -uo pipefail
cd "$(dirname "$0")/.."

QUICK=0
for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

echo "== cargo test (workspace) =="
cargo test --workspace
TEST_RC=$?
if [ "$TEST_RC" -ne 0 ]; then
  echo "FAIL: cargo test failed ($TEST_RC)" >&2
  exit 1
fi

if [ "$QUICK" -eq 0 ]; then
  echo "== cargo clippy --workspace --all-targets (deny warnings) =="
  if ! cargo clippy --workspace --all-targets -- -D warnings; then
    echo "FAIL: clippy produced warnings/errors" >&2
    exit 1
  fi
fi

echo "== differential verification over every fixture (exhaustive) =="
OVERALL=0
for f in fixtures/*.ial; do
  echo "-- verifying $f"
  OUTPUT=$(./target/debug/ia verify --config config/verify.default.json "$f" 2>/dev/null)
  RC=$?
  echo "$OUTPUT" | sed 's/^/   /'
  if echo "$OUTPUT" | grep -q "NOT run"; then
    echo "FAIL: evidence incomplete (NOT run) for $f" >&2
    OVERALL=5
  elif [ "$RC" -eq 4 ]; then
    echo "FAIL: UNSOUND for $f" >&2
    OVERALL=4
  elif [ "$RC" -ne 0 ]; then
    echo "FAIL: verifier exited $RC for $f" >&2
    OVERALL=1
  fi
done

if [ "$QUICK" -eq 0 ]; then
  echo "== cargo build --release =="
  if ! cargo build --release; then
    echo "FAIL: release build failed" >&2
    exit 1
  fi
fi

if [ "$OVERALL" -eq 0 ]; then
  echo "ALL CHECKS PASSED"
fi
exit "$OVERALL"
