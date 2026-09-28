#!/usr/bin/env bash
# smoke.sh — boots the real dhcp4d on loopback with a throwaway SQLite
# file and drives a complete lifecycle through the HTTP replay API using
# an INDEPENDENT datagram assembler (Python, no shared code with the Go
# implementation). Concrete outcomes are asserted:
#
#   DISCOVER -> OFFER (reservation only, no lease row)
#   REQUEST(selecting) -> ACK (leased row, expires in the future)
#   duplicate REQUEST -> ACK with duplicate=true and SAME expiry
#   RELEASE -> released (no reply)
#   bad hex  -> 422 with a fail_category (never a success)
#
# Usage: smoke.sh /path/to/dhcp4d [logfile]
set -euo pipefail

BIN="${1:?dhcp4d binary path required}"
LOG="${2:-/dev/stderr}"
WORK="$(mktemp -d)"
trap 'set +e; kill "$SRV_PID" >/dev/null 2>&1; wait "$SRV_PID" 2>/dev/null; rm -rf "$WORK"' EXIT

read -r PORT UDPPORT < <(python3 - <<'PY'
import socket
def free_tcp():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
def free_udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p
# Pick independently; retry until they differ and neither collides.
t, u = free_tcp(), free_udp()
while u == t:
    u = free_udp()
print(t, u)
PY
)

cat >"$WORK/lab.json" <<JSON
{
  "listen_udp": "127.0.0.1:$UDPPORT",
  "admin_http": "127.0.0.1:$PORT",
  "allow_non_loopback": false,
  "server_id": "192.0.2.1",
  "network": "192.0.2.0/24",
  "pool_start": "192.0.2.10",
  "pool_end": "192.0.2.40",
  "netmask": "255.255.255.0",
  "router": "192.0.2.1",
  "dns": ["192.0.2.53"],
  "lease_time": 120,
  "offer_ttl": 30,
  "sweep_interval": 1,
  "database": "file:$WORK/smoke.db"
}
JSON

echo "[smoke] config=$WORK/lab.json http=127.0.0.1:$PORT" >"$LOG"
"$BIN" -config "$WORK/lab.json" -text-logs >>"$LOG" 2>&1 &
SRV_PID=$!

# Wait for health.
for i in $(seq 1 50); do
  if curl -sf "http://127.0.0.1:$PORT/api/v1/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
  if [ "$i" -eq 50 ]; then
    echo "[smoke] server did not become healthy" >&2
    exit 1
  fi
done

export SMOKE_URL="http://127.0.0.1:$PORT"
exec python3 - "$LOG" <<'PY'
import json, os, struct, sys, time, urllib.request, urllib.error

logf = sys.argv[1]
def note(msg):
    line = f"[smoke] {msg}"
    print(line)
    with open(logf, "a") as f:
        f.write(line + "\n")

URL = os.environ["SMOKE_URL"]
COOKIE = bytes([99, 130, 83, 99])

# --- Independent BOOTP assembler (separate code from the Go server) ---
def build(mtype, xid, chaddr=b"\x02\x00\x00\x00\x00\x01",
          ciaddr=b"\x00\x00\x00\x00", opts_extra=b""):
    b = bytearray(300)
    b[0] = 1                      # BOOTREQUEST
    b[1], b[2] = 1, 6             # Ethernet / hlen 6
    b[4:8] = xid
    b[12:16] = ciaddr
    b[44:50] = chaddr
    b[236:240] = COOKIE
    pos = 240
    # option 53 message type
    b[pos:pos+3] = bytes([53, 1, mtype]); pos += 3
    b[pos:pos+len(opts_extra)] = opts_extra; pos += len(opts_extra)
    b[pos] = 255
    return bytes(b)

def opt50(ip):   return bytes([50, 4]) + ip
def opt54(ip):   return bytes([54, 4]) + ip

def parse(p):
    # minimal independent field extraction for assertions
    out = {
        "op": p[0], "yiaddr": ".".join(str(x) for x in p[20:24]),
        "siaddr": ".".join(str(x) for x in p[24:28]),
        "opts": {},
    }
    i = 240
    assert bytes(p[236:240]) == COOKIE, "bad cookie in server reply"
    while i < len(p):
        code = p[i]; i += 1
        if code == 0: continue
        if code == 255: break
        ln = p[i]; i += 1
        out["opts"][code] = bytes(p[i:i+ln]); i += ln
    return out

def inject(dg):
    body = json.dumps({"datagram_hex": dg.hex()}).encode()
    req = urllib.request.Request(URL + "/api/v1/inject", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

def inject_raw(text):
    body = json.dumps({"datagram_hex": text}).encode()
    req = urllib.request.Request(URL + "/api/v1/inject", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)

failures = []
def check(cond, desc):
    note(("PASS " if cond else "FAIL ") + desc)
    if not cond:
        failures.append(desc)

xid = bytes([0x0a, 0x0b, 0x0c, 0x0d])
server_id = bytes([192, 0, 2, 1])

# 1. DISCOVER -> OFFER
st, r = inject(build(1, xid))
check(st == 200 and r["action"] == "offer" and r["reply_type"] == "OFFER",
      f"DISCOVER yields OFFER (http={st}, action={r.get('action')})")
offer = parse(bytes.fromhex(r["reply_hex"]))
check(offer["yiaddr"].startswith("192.0.2."), f"OFFER yiaddr in subnet: {offer['yiaddr']}")
check(offer["opts"].get(54) == server_id, "OFFER option54 == server id")
check(struct.unpack(">I", offer["opts"][51])[0] == 120, "OFFER option51 == 120s")
offered = bytes(int(x) for x in offer["yiaddr"].split("."))

# OFFER is a reservation: leases table must be empty.
with urllib.request.urlopen(URL + "/api/v1/leases") as lr:
    leases = json.load(lr)["leases"]
check(leases == [], f"no leased row after OFFER (rows={len(leases)})")

# 2. SELECTING REQUEST -> ACK
req_dg = build(3, xid, opts_extra=opt50(offered) + opt54(server_id))
st, r = inject(req_dg)
check(st == 200 and r["action"] == "ack" and r["reply_type"] == "ACK",
      f"SELECTING REQUEST yields ACK (action={r.get('action')}, reason={r.get('reason','')})")
check(r["lease_state"] == "leased", f"lease_state=leased (got {r.get('lease_state')})")
check(r["lease_ip"] == offer["yiaddr"], "ACK lease_ip matches OFFER")
expiry1 = r["lease_expires"]
ack = parse(bytes.fromhex(r["reply_hex"]))
check(ack["yiaddr"] == offer["yiaddr"], "ACK yiaddr matches OFFER")
check(ack["opts"].get(1) == bytes([255, 255, 255, 0]), "ACK carries netmask")

# 3. Duplicate REQUEST -> duplicate=true, SAME expiry (no extension).
time.sleep(1.05)  # wall clock moves; stored expiry must not
st, r = inject(req_dg)
check(r.get("duplicate") is True, "duplicate REQUEST flagged duplicate")
check(r["lease_expires"] == expiry1,
      f"duplicate ACK does NOT extend lease ({expiry1} == {r['lease_expires']})")

# 4. RELEASE -> released, no reply bytes.
rel_dg = build(7, bytes([0x0a,0x0b,0x0c,0x0e]), ciaddr=offered)
st, r = inject(rel_dg)
check(r["action"] == "released" and r["reply_bytes"] == 0,
      f"RELEASE yields 'released' with no reply (action={r['action']}, reply_bytes={r['reply_bytes']})")

# 5. Malformed input -> 422 + fail_category, never a success.
st, r = inject_raw("aa" * 40)
check(st == 422 and "fail_category" in r,
      f"truncated datagram -> 422 classified (http={st}, cat={r.get('fail_category')})")

# 6. Version endpoint reports build identity.
with urllib.request.urlopen(URL + "/api/v1/version") as vr:
    v = json.load(vr)
check(v["server_version"] and v["protocol"] == "dhcp4-subset/rfc2131",
      f"version reports identity: {v.get('server_version')} {v.get('protocol')}")

if failures:
    note(f"{len(failures)} ASSERTION(S) FAILED")
    sys.exit(2)
note("ALL SMOKE ASSERTIONS PASSED")
PY
