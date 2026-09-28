#!/usr/bin/env bash
# Service call examples against a locally running cidrsvc (default 127.0.0.1:8080).
# Every request carries an explicit X-Request-Id so results and logs can be
# correlated and replayed. Requires only curl.
set -u
BASE="${BASE:-http://127.0.0.1:8080}"
say() { printf '\n===== %s =====\n' "$1"; }

say "1) health"
curl -sS "$BASE/healthz" | head -c 400; echo

say "2) IPv4: carve one /25 out of a /24 (minimal cover)"
curl -sS -X POST "$BASE/v1/compute" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-ipv4-hole' \
  -H 'X-Client-Ref: runbook-example-2' \
  -d '{"family":"ipv4","allow":["10.0.0.0/24"],"exclude":["10.0.0.128/25"]}'
echo

say "3) full IPv6 space minus the lowest address (extreme boundary)"
curl -sS -X POST "$BASE/v1/compute" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-v6-extreme' \
  -d '{"family":"ipv6","allow":["::/0"],"exclude":["::/128"]}' \
  | head -c 700; echo

say "4) empty set: exclude the whole universe"
curl -sS -X POST "$BASE/v1/compute" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-empty' \
  -d '{"family":"ipv4","allow":["0.0.0.0/0"],"exclude":["0.0.0.0/0"]}'
echo

say "5) error case: prefix length too long (typed failure)"
curl -sS -X POST "$BASE/v1/compute" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-bad-prefix' \
  -d '{"family":"ipv4","allow":["10.0.0.0/33"]}'
echo

say "6) error case: mixed address families"
curl -sS -X POST "$BASE/v1/compute" \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-mixed' \
  -d '{"allow":["10.0.0.0/8"],"exclude":["2001:db8::/32"]}'
echo

say "7) replay one known request by correlation id"
curl -sS "$BASE/v1/requests/demo-ipv4-hole"
echo

say "8) list recent failed requests"
curl -sS "$BASE/v1/requests?status=error&limit=10"
echo

say "9) aggregate stats"
curl -sS "$BASE/v1/stats"
echo
