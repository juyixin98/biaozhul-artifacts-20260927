#!/usr/bin/env bash
# Build and run both services locally with the bundled example config.
set -euo pipefail
cd "$(dirname "$0")/.."

export $(grep -v '^#' configs/config.env | grep -v '^$' | xargs)

mkdir -p data bin
go build -o bin/fakeservice ./cmd/fakeservice
go build -o bin/controller ./cmd/controller

cleanup() {
  kill "$FAKE_PID" "$CTRL_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

./bin/fakeservice &
FAKE_PID=$!
sleep 0.5
./bin/controller &
CTRL_PID=$!

echo
echo "controller:  $RC_HTTP_ADDR   (API: /api/v1/widgets)"
echo "fake cloud:  $FAKE_HTTP_ADDR (fault control: /internal/faults/{op})"
echo "press Ctrl+C to stop"
wait
