#!/usr/bin/env bash
# Self-contained deterministic demo of "external create succeeds but its
# response is lost": the controller must query/claim the committed resource
# and must NOT create a duplicate.
#
# It starts/its own three binaries on fresh data, arms the fault BEFORE the
# controller exists, then starts the controller and observes the claim.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
B=http://127.0.0.1:19091
A=http://127.0.0.1:19092
D=http://127.0.0.1:19093
DEMO_DIR=$(mktemp -d /tmp/crc-lostcreate-XXXXXX)
trap 'kill ${API_PID:-0} ${ACT_PID:-0} ${CTL_PID:-0} 2>/dev/null || true' EXIT

cat >"$DEMO_DIR/api.json" <<JSON
{"listen":"127.0.0.1:19091","databasePath":"$DEMO_DIR/desired.db",
 "controllerAuth":"local-dev-secret","disableEventSink":true}
JSON
cat >"$DEMO_DIR/actual.json" <<JSON
{"listen":"127.0.0.1:19092","databasePath":"$DEMO_DIR/actual.db"}
JSON
cat >"$DEMO_DIR/ctl.json" <<JSON
{"desiredURL":"$B","actualURL":"$A","controllerAuth":"local-dev-secret",
 "databasePath":"$DEMO_DIR/controller.db","diagnosticsAddr":"127.0.0.1:19093",
 "resyncInterval":"300ms","backoffBaseMS":20,"backoffMaxMS":200}
JSON

go build -o "$DEMO_DIR/apiserver" ./cmd/apiserver
go build -o "$DEMO_DIR/actualserver" ./cmd/actualserver
go build -o "$DEMO_DIR/controller" ./cmd/controller

"$DEMO_DIR/actualserver" -config "$DEMO_DIR/actual.json" >"$DEMO_DIR/actual.log" 2>&1 &
ACT_PID=$!
"$DEMO_DIR/apiserver" -config "$DEMO_DIR/api.json" >"$DEMO_DIR/api.log" 2>&1 &
API_PID=$!
sleep 1

echo "== create object while NO controller exists =="
curl -s -X POST "$B/api/v1/namespaces/demo/resources" -H 'Content-Type: application/json' \
  -d '{"name":"lost-one","spec":{"replicas":1}}' >"$DEMO_DIR/create.json"
OBJ_UID=$(python3 -c 'import json;print(json.load(open("'$DEMO_DIR'/create.json"))["uid"])')
echo "uid=$OBJ_UID"
python3 -c 'import json;o=json.load(open("'$DEMO_DIR'/create.json"));print("  finalizers:",o["finalizers"],"observedGen:",o["status"]["observedGeneration"])'

echo "== arm create-response-loss BEFORE starting controller =="
curl -s -X POST "$A/admin/faults" -H 'Content-Type: application/json' \
  -d "{\"ownerUID\":\"$OBJ_UID\",\"fault\":\"create-response-loss\"}" >/dev/null
echo "  fault map: $(curl -s "$A/admin/faults")"

"$DEMO_DIR/controller" -config "$DEMO_DIR/ctl.json" >"$DEMO_DIR/ctl.log" 2>&1 &
CTL_PID=$!
sleep 1.5

echo "== after first reconcile: row committed; lost POST resolved by claim =="
echo "  counters: $(curl -s "$A/admin/counters")"
OG=$(curl -s "$B/api/v1/namespaces/demo/resources/lost-one" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"]["observedGeneration"])')
# The fault only drops the POST response; the follow-up by-owner GET still
# works, so the controller claims in the same round and converges to 1.
# The key invariant below is that exactly ONE physical row exists.
echo "  observedGeneration=$OG (claimed via by-owner query; not a recreate)"
NROWS=$(curl -s "$A/admin/resources" | python3 -c 'import sys,json
rows=[r for r in json.load(sys.stdin) if r["ownerUID"]=="'$OBJ_UID'"]
print(len(rows))')
echo "  physical rows committed despite lost response=$NROWS (must be 1)"

echo "== clear fault: controller claims by owner, never recreates =="
curl -s -X DELETE "$A/admin/faults/$OBJ_UID" >/dev/null
for _ in $(seq 1 40); do
  OG=$(curl -s "$B/api/v1/namespaces/demo/resources/lost-one" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"]["observedGeneration"])')
  [ "$OG" = "1" ] && break; sleep 0.2
done
echo "  observedGeneration=$OG (must be 1)"
echo "  counters after claim: $(curl -s "$A/admin/counters")"
NROWS2=$(curl -s "$A/admin/resources" | python3 -c 'import sys,json
rows=[r for r in json.load(sys.stdin) if r["ownerUID"]=="'$OBJ_UID'"]
print(len(rows))')
echo "  physical rows=$NROWS2 (must stay 1 — no duplicate create)"

echo "== decision ledger (accepted / undecidable / claim) =="
curl -s "$D/diagnostics/ledger/$OBJ_UID" | python3 -c '
import sys,json
for e in json.load(sys.stdin):
    print("  %-9s %-22s cat=%-13s req=%s" %
          (e["phase"], e["decision"], e.get("category") or "-", e["requestID"][:18]))
    print("      ", e["detail"][:90])'

# Hard assertions so the script fails loudly if behavior regresses.
[ "$OG" = "1" ] || { echo "FAIL: did not converge"; exit 1; }
[ "$NROWS" = "1" ] || { echo "FAIL: row not committed during loss"; exit 1; }
[ "$NROWS2" = "1" ] || { echo "FAIL: duplicate resource created"; exit 1; }
echo
echo "LOST-CREATE DEMO OK (logs in $DEMO_DIR)"
