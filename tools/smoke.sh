#!/usr/bin/env bash
# End-to-end smoke demo: builds, round-trips every sample shape through the
# CLI, and shows diagnostics for a deliberately corrupted container.
set -euo pipefail
cd "$(dirname "$0")/.."

BIN=target/release/rangecode
cargo build --release >/dev/null

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

run_case() {
  local name=$1 file=$2 mode=$3
  printf '%-22s ' "$name"
  $BIN encode $mode "$file" "$work/$name.rcmp" >/dev/null
  $BIN decode "$work/$name.rcmp" "$work/$name.out" >/dev/null
  if cmp -s "$file" "$work/$name.out"; then
    printf 'round-trip OK  (%d -> %d container bytes)\n' \
      "$(wc -c < "$file")" "$(wc -c < "$work/$name.rcmp")"
  else
    echo "ROUND-TRIP MISMATCH"; exit 1
  fi
}

echo "== CLI round-trips =="
run_case repetitive samples/repetitive.txt "--mode adaptive --chunk 256"
run_case alternating  samples/alternating.txt  "--mode static"
run_case all-bytes    samples/all-bytes.bin    "--mode adaptive"
run_case one-byte     samples/one-byte.bin     "--mode static"
run_case short        samples/short.txt        "--freq samples/freq-text.json"

echo
echo "== empty input =="
: > "$work/empty"
$BIN encode "$work/empty" "$work/empty.rcmp" >/dev/null
$BIN verify "$work/empty.rcmp"

echo
echo "== precise rejection of a truncated container =="
$BIN encode samples/short.txt "$work/s.rcmp" >/dev/null
head -c 20 "$work/s.rcmp" > "$work/s-trunc.rcmp" || true
if $BIN verify "$work/s-trunc.rcmp"; then
  echo "ERROR: truncation accepted"; exit 1
else
  echo "(exit non-zero as required)"
fi

echo
echo "== inspect =="
$BIN inspect "$work/repetitive.rcmp" | head -20

echo
echo "ALL SMOKE CHECKS PASSED"
