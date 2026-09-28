#!/usr/bin/env bash
# Local end-to-end demo: starts netsemd, submits the demo config, prints the
# analysis report, evaluates representative packets, and shows one
# correlated, explainable request log. All artifacts are local and synthetic.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ADDR="127.0.0.1:$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
DB="$(mktemp /tmp/netsem-demo-XXXXXX.db)"
cleanup() { kill "${SRV_PID:-0}" 2>/dev/null || true; rm -f "$DB" "$DB-wal" "$DB-shm" /tmp/netsem-last.json; }
trap cleanup EXIT

cd "$ROOT"
echo ">> building server"
go build -o /tmp/netsemd ./cmd/server

echo ">> starting server on $ADDR (db=$DB)"
/tmp/netsemd -addr "$ADDR" -db "$DB" -instance demo-local &
SRV_PID=$!
for _ in $(seq 1 50); do
  curl -sf "http://$ADDR/healthz" >/dev/null && break
  sleep 0.1
done
base="http://$ADDR"

echo
echo ">> 1) submit config/demo_rules.json (diagnostics section)"
curl -s -XPOST "$base/configs" -H 'Content-Type: application/json' \
  --data-binary @"$ROOT/config/demo_rules.json" > /tmp/netsem-cfg.json
python3 - "$base" <<'PY'
import json, sys
d = json.load(open("/tmp/netsem-cfg.json"))
print("parse_ok =", d["parse_ok"], " version =", d["version"])
for n in d.get("notes", []):
    print("note[%s]: %s" % (n["rule_id"], n["note"]))
for x in d["report"]["diagnostics"]:
    w = x.get("witness") or {}
    print("- %-22s %-5s %-18s witness=%s:%d -> matched %s" % (
        x["rule_id"], x["family"], x["kind"],
        w.get("destination_address", ""), w.get("destination_port", -1),
        w.get("matched_rule_id", "")))
PY

echo
echo ">> 2) evaluate packets"
eval_pkt() {
  local name="$1" body="$2"
  curl -s -XPOST "$base/evaluate" -H 'Content-Type: application/json' -d "$body" > /tmp/netsem-last.json
  python3 - "$name" <<'PY'
import json, sys
name = sys.argv[1]
d = json.load(open("/tmp/netsem-last.json"))
steps = d.get("steps") or []
print("--- %s" % name)
print("    decision=%s decided_by=%s request_id=%s" % (d["decision"], d["decided_by"], d["request_id"]))
if steps:
    last = steps[-1]
    print("    steps=%d; final=%s/%s" % (len(steps), last["rule_id"], last.get("action", "")))
else:
    print("    steps=0 (rejected before rule evaluation)")
for u in d.get("uncertainties") or []:
    print("    uncertainty:", u)
for e in d.get("errors") or []:
    print("    error:", e)
PY
}

eval_pkt "shadowed-witness (tcp :85 -> allowed by first rule)" \
  '{"family":"ipv4","protocol":"tcp","source_address":"10.1.0.5","destination_address":"10.2.0.9","source_port":40000,"destination_port":85}'
eval_pkt "live-partition (tcp :95 -> first rule still governs)" \
  '{"family":"ipv4","protocol":"tcp","source_address":"10.1.0.5","destination_address":"10.2.0.9","source_port":40000,"destination_port":95}'
eval_pkt "redundant-udp53 (default deny)" \
  '{"family":"ipv4","protocol":"udp","source_address":"10.1.0.5","destination_address":"10.2.0.9","source_port":40000,"destination_port":53}'
eval_pkt "ipv6 allow (tcp :443)" \
  '{"family":"ipv6","protocol":"tcp","source_address":"2001:db8:1::1","destination_address":"2001:db8:2::1","source_port":5000,"destination_port":443}'
eval_pkt "unknown numeric protocol 99 (uncertain)" \
  '{"family":"ipv4","protocol":"99","source_address":"198.51.100.7","destination_address":"203.0.113.9","source_port":0,"destination_port":0}'
eval_pkt "invalid input (hard error, listed separately)" \
  '{"family":"ipv4","protocol":"tcp","source_address":"not-an-ip","destination_address":"10.0.0.1"}'

echo
echo ">> 3) correlated log of the last request (identity, version, steps, location)"
REQ="$(python3 -c 'import json;print(json.load(open("/tmp/netsem-last.json"))["request_id"])')"
curl -s "$base/requests/$REQ" | python3 -m json.tool
