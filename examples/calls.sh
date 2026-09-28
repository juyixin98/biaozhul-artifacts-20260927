#!/usr/bin/env bash
# End-to-end example calls for a locally running ribd (SQLite mode by default).
# Start it first:
#   go run ./cmd/ribd -config testdata/config/example.json
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8080}

say() { printf '\n### %s\n' "$1"; }

say "health"
curl -fsS "$BASE/healthz"; echo

say "current version (bootstrap routes appear atomically at v1)"
curl -fsS "$BASE/v1/version"; echo

say "default route resolves recursively via 203.0.113.1 -> connected eth1"
curl -fsS "$BASE/v1/lookup?target=192.0.2.5" | python3 -m json.tool

say "install a self-loop and a two-node cycle in one dry-run-validated batch"
curl -fsS -XPOST "$BASE/v1/batches" -H 'Content-Type: application/json' -d '{
  "changes": [
    {"kind":"upsert","route":{"id":"loop-self","prefix":"198.18.0.1/32","admin_distance":5,
     "next_hop":{"addr":"198.18.0.1"}}},
    {"kind":"upsert","route":{"id":"cyc-a","prefix":"198.19.0.1/32","admin_distance":5,
     "next_hop":{"addr":"198.19.0.2"}}},
    {"kind":"upsert","route":{"id":"cyc-b","prefix":"198.19.0.2/32","admin_distance":5,
     "next_hop":{"addr":"198.19.0.1"}}}
  ]}' | python3 -m json.tool

say "self loop -> rejected/loop"
curl -fsS "$BASE/v1/lookup?target=198.18.0.1" | python3 -m json.tool

say "two-node cycle -> rejected/loop (3-step chain)"
curl -fsS "$BASE/v1/lookup?target=198.19.0.1" | python3 -m json.tool

say "IPv6 compressed input normalizes; no v6 next-hop cover -> indeterminate unless connected route covers it"
curl -fsS "$BASE/v1/lookup?target=2001:db8:dead::beef" | python3 -m json.tool

say "invalid query -> rejected/bad_query"
curl -fsS "$BASE/v1/lookup?target=10.0.0.0/8" | python3 -m json.tool

say "event stream and independent replay with snapshot verification"
curl -fsS "$BASE/v1/events" | python3 -m json.tool
curl -fsS -XPOST "$BASE/v1/replay" -H 'Content-Type: application/json' -d '{"verify":true}' \
  | python3 -m json.tool

say "routes listing redacts secret-token"
count=$(curl -fsS "$BASE/v1/routes" | grep -c 'REDACTED' || true)
echo "redacted fields: $count"
