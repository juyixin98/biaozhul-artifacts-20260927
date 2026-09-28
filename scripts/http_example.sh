#!/usr/bin/env bash
# Example end-to-end HTTP calls against `igmpv2timer serve`.
# Uses only the standard library server + a local synthetic host. No real
# network traffic is produced; time is the injected virtual clock (ms).
set -euo pipefail

BASE="${BASE:-http://127.0.0.1:18022}"

post() { # $1=path, rest=JSON body
  curl -sS -X POST "$BASE$1" -H 'Content-Type: application/json' "$@"
}

echo "== health =="
curl -sS "$BASE/healthz"; echo

echo "== hostA reports membership for 239.5.5.5 at t=100 =="
post /events -H 'X-Request-ID: join-42' \
  -d '{"at_ms":100,"kind":"report","iface":"eth0","group":"239.5.5.5","member":"hostA","source_addr":"192.0.2.11"}'; echo

echo "== a malformed (unicast) group is REJECTED (422) with a reason/req id =="
post /events \
  -d '{"at_ms":110,"kind":"report","iface":"eth0","group":"10.0.0.1","member":"hostA","source_addr":"192.0.2.11"}' \
  -w '\n[HTTP %{http_code}]\n'

echo "== a General Query at t=150 opens generation 1 =="
post /events -d '{"at_ms":150,"kind":"general_query","iface":"eth0"}'; echo

echo "== advance the clock to t=400 -> membership interval expires =="
post /tick -d '{"to_ms":400}'; echo

echo "== forwarding table is now empty =="
curl -sS "$BASE/state"; echo

echo "== intervals retained across the run =="
curl -sS "$BASE/intervals"; echo
