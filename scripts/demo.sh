#!/usr/bin/env bash
# End-to-end demo. Start the server first with:
#   go run ./cmd/server -config configs/demo.json
# Then: BASE=http://127.0.0.1:8081 ./scripts/demo.sh
# Uses only curl. Every response carries request_id + the budget snapshot.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8081}"

say() { printf '\n### %s\n' "$1"; }
post() { # post <path> <json-string|@file> [request-id]
  local path="$1" data="$2" rid="${3:-}"
  local args=(-sS -X POST "$BASE$path" -H 'Content-Type: application/json' --data-binary "$data")
  [[ -n "$rid" ]] && args+=(-H "X-Request-ID: $rid")
  curl "${args[@]}" -w '\n[http %{http_code}]\n'
}

say "1. health"
curl -sS "$BASE/healthz" -w '\n[http %{http_code}]\n'

say "2. create group web: 3 replicas, maxUnavailable=1 (selector epoch starts at 1)"
post /api/v1/groups @requests/01-create-group.json demo-create

say "3. report all three members ready at epoch 1"
post /api/v1/observations @requests/02-observe-a-ready.json
post /api/v1/observations @requests/02-observe-b-ready.json
post /api/v1/observations @requests/02-observe-c-ready.json

say "4. evict a -> 202 accepted; the single slot is now reserved"
post /api/v1/namespaces/default/groups/web/eviction @requests/03-evict-a.json demo-evict-a | tee /tmp/ec-evict-a.json
APPR=$(sed -n 's/.*"approval_id":"\([^"]*\)".*/\1/p' /tmp/ec-evict-a.json | head -1)
echo "approval id: $APPR"

say "5. immediate duplicate-style eviction of b -> 422 budget_exhausted"
post /api/v1/namespaces/default/groups/web/eviction @requests/04-evict-b-exhausted.json demo-evict-b

say "6. replaying the SAME request id returns the same approval (no second slot)"
post /api/v1/namespaces/default/groups/web/eviction @requests/03-evict-a.json demo-evict-a

say "7. report the eviction succeeded -> reservation released"
post "/api/v1/approvals/$APPR/result" @requests/06-result-succeeded.json

say "8. evict b is still blocked: a is counted not-ready until a fresh ready observation"
post /api/v1/namespaces/default/groups/web/eviction @requests/04-evict-b-exhausted.json demo-evict-b2
post /api/v1/observations @requests/02-observe-a-ready.json

say "9. approve b, then let it EXPIRE (demo TTL=5s) WITHOUT finishing it"
post /api/v1/namespaces/default/groups/web/eviction @requests/04-evict-b-exhausted.json demo-evict-b3 | tee /tmp/ec-evict-b.json
APPR_B=$(sed -n 's/.*"approval_id":"\([^"]*\)".*/\1/p' /tmp/ec-evict-b.json | head -1)
echo "approval b: $APPR_B; sleeping 7s for the expiry sweep..."
sleep 7
curl -sS "$BASE/api/v1/approvals/$APPR_B" -w '\n[http %{http_code}]\n'

say "10. after expiry the slot is STILL charged -> reclaimable list shows it"
curl -sS "$BASE/api/v1/reclaimable?namespace=default&group=web" -w '\n[http %{http_code}]\n'
post /api/v1/namespaces/default/groups/web/eviction @requests/03-evict-a.json demo-evict-postexpiry

say "11. reclaim WITHOUT confirm is refused; nothing released"
curl -sS -X POST "$BASE/api/v1/approvals/$APPR_B/reclaim" -H 'Content-Type: application/json' \
  --data '{"confirm":false}' -w '\n[http %{http_code}]\n'

say "12. confirmed reclaim -> slot returns; a can be evicted again"
post "/api/v1/approvals/$APPR_B/reclaim" @requests/07-reclaim-confirm.json
post /api/v1/namespaces/default/groups/web/eviction @requests/03-evict-a.json demo-evict-reapproved

say "13. selector change versions to epoch 2; stale client epoch is rejected as such"
curl -sS -X POST "$BASE/api/v1/groups/default/web/selectors:bump" \
  -H 'Content-Type: application/json' --data '{}' -w '\n[http %{http_code}]\n'
curl -sS -X POST "$BASE/api/v1/namespaces/default/groups/web/eviction" \
  -H 'Content-Type: application/json' --data '{"instance_id":"c","client_selector_epoch":1}' \
  -w '\n[http %{http_code}]\n'

say "14. involuntary failure recorded; requesting its eviction is instance_failed, NOT budget-blocked"
post /api/v1/failures @requests/05-failure-a.json
# restore a fresh current-epoch (epoch 2 now) observation first, then fail
curl -sS -X POST "$BASE/api/v1/observations" -H 'Content-Type: application/json' \
  --data '{"instance":{"id":"a","namespace":"default","group":"web","labels":{"app":"web"}},"ready":true,"epoch":2}' >/dev/null
curl -sS -X POST "$BASE/api/v1/failures" -H 'Content-Type: application/json' \
  --data '{"instance_id":"a","reason":"node-lost","epoch":2}' -w '\n[http %{http_code}]\n'
curl -sS -X POST "$BASE/api/v1/namespaces/default/groups/web/eviction" \
  -H 'Content-Type: application/json' --data '{"instance_id":"a"}' \
  -w '\n[http %{http_code}]\n'
