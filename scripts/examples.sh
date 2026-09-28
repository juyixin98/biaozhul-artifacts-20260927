#!/usr/bin/env bash
# End-to-end smoke script against two locally running binaries.
# Usage:
#   make run          (in two terminals, or use ./scripts/dev.sh)
#   ./scripts/examples.sh
set -euo pipefail

B="${RC_HTTP_ADDR:-http://127.0.0.1:8080}"
case "$B" in http://*) ;; *) B="http://$B";; esac
F="${RC_EXTERNAL_URL:-http://127.0.0.1:8090}"

say() { printf '\n=== %s ===\n' "$*"; }

# Make the script re-runnable: clear injected faults and remove resources
# created by a previous run (waiting for cleanup to finish).
curl -s -X DELETE "$F/internal/faults" >/dev/null || true
for n in demo lost; do
  curl -s -o /dev/null -X DELETE "$B/api/v1/widgets/$n" || true
done
for _ in $(seq 1 40); do
  for n in demo lost; do
    curl -s -o /dev/null "$B/api/v1/widgets/$n" || true
  done
  left=0
  for n in demo lost; do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$B/api/v1/widgets/$n")" = "404" ] || left=1
  done
  [ "$left" = 0 ] && break
  sleep 0.3
done

say "health"
curl -fsS "$B/healthz" && echo
curl -fsS "$F/healthz" && echo

say "create desired resource (generation 1)"
curl -fsS -X POST "$B/api/v1/widgets" \
  -H 'Content-Type: application/json' \
  -d '{"metadata":{"name":"demo"},"spec":{"replicas":3,"color":"blue","secretToken":"demo-secret-1"}}'
echo

sleep 1
say "observed controller state after convergence"
curl -fsS "$B/api/v1/widgets/demo" | python3 -m json.tool

EXT=$(curl -fsS "$B/api/v1/widgets/demo" |
  python3 -c 'import json,sys;print(json.load(sys.stdin)["status"]["externalId"])')
say "actual resource service state for $EXT"
curl -fsS "$F/v1/widgets/$EXT" | python3 -m json.tool

say "stale update is rejected (If-Match: 1)"
curl -s -o /dev/null -w "HTTP %{http_code} (want 409)\n" \
  -X PUT "$B/api/v1/widgets/demo" \
  -H 'Content-Type: application/json' -H 'If-Match: 1' \
  -d '{"spec":{"replicas":9,"color":"black"}}'

say "inject: create response lost, then create again"
curl -fsS -X PUT "$F/internal/faults/create" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"createResponseLost","times":1}'
echo
curl -fsS -X POST "$B/api/v1/widgets" \
  -H 'Content-Type: application/json' \
  -d '{"metadata":{"name":"lost"},"spec":{"replicas":1,"color":"red"}}'
echo
sleep 1
curl -fsS "$B/api/v1/widgets/lost" |
  python3 -c 'import json,sys; d=json.load(sys.stdin); print("reconciledGeneration:", d["status"]["reconciledGeneration"], "phase:", d["status"]["phase"])'
echo "object count in actual service (want 2):"
curl -fsS "$F/v1/widgets" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["items"]))'

say "inject: one failed delete, then request deletion"
curl -fsS -X PUT "$F/internal/faults/delete" \
  -H 'Content-Type: application/json' -d '{"kind":"deleteFail","times":1}'
echo
curl -s -o /dev/null -w "delete API HTTP %{http_code} (want 202)\n" \
  -X DELETE "$B/api/v1/widgets/demo"
for _ in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$B/api/v1/widgets/demo" || true)
  [ "$code" = "404" ] && { echo "record removed after confirmed cleanup"; break; }
  sleep 0.3
done

say "final actual-service state"
curl -fsS "$F/internal/status" | python3 -m json.tool
