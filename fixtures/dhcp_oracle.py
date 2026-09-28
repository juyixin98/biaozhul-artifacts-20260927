#!/usr/bin/env python3
"""
dhcp_oracle.py — independent reference oracle for the dhcpv4lab server.

This program is deliberately implemented from the protocol text (RFC 2131) in
Python with the standard library only. It does NOT import or call any code
under internal/. Its packet codec is written independently so that a shared
encoding bug between fixture and implementation cannot hide a defect.

It drives a *running* server over raw loopback UDP (default port 10067) and
optionally uses the HTTP diagnostics API to verify persisted lease state.

Scenarios (each asserts a concrete outcome and a specific failure category):

  1 dora                DISCOVER -> OFFER -> REQUEST(selecting) -> ACK
  2 duplicate_no_extend identical DISCOVER/REQUEST are replayed byte-for-byte;
                        retransmission never moves lease boundaries
  3 old_xid_replay      a REQUEST carrying an ancient xid but a never-offered
                        address is NAKed as nak_no_valid_offer
  4 init_reboot         client restart: option50, no server-id; lease
                        boundaries must remain unchanged
  5 expired_release     advance fake clock past offer TTL / lease; stale
                        REQUEST is NAKed, lease expires, address reallocated
  6 contention          N parallel clients against a small pool: every
                        address is owned by exactly one client
  7 release_ownership   a stranger cannot RELEASE another client's lease;
                        the owner's release frees the address
  8 malformed           truncated / bad-cookie datagrams get no reply;
                        unsupported message types are not reported as success

Usage:
  python3 fixtures/dhcp_oracle.py --udp 127.0.0.1:10067 --http http://127.0.0.1:18080 \
      --run-id oracle-<timestamp>

Exit status: 0 only when every assertion passes. A report (JSON + text) is
written to stdout and to --report (default oracle_report.json).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# ---- protocol constants (copied from RFC 2131, independent of Go code) ----
MAGIC = bytes([99, 130, 83, 99])
OP_REQUEST, OP_REPLY = 1, 2
DISCOVER, OFFER, REQUEST, ACK, NAK, RELEASE, DECLINE, INFORM = 1, 2, 3, 5, 6, 7, 4, 8
O_SUBNET, O_ROUTER, O_DNS, O_REQ_IP, O_LEASE, O_MSG_TYPE, O_SERVER_ID, O_MESSAGE, O_CLIENT_ID = (
    1, 3, 6, 50, 51, 53, 54, 56, 61)


def log(step: str, msg: str, **fields) -> None:
    """Structured progress line tying the decision to run identity/input."""
    payload = " ".join(f"{k}={v}" for k, v in fields.items())
    print(f"[{time.strftime('%H:%M:%S')}] [{step}] {msg} {payload}".rstrip(), flush=True)


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    expected: str = ""
    observed: str = ""


@dataclass
class Report:
    run_id: str
    version: str = ""
    checks: list = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str, expected: str = "", observed: str = "") -> None:
        c = Check(name, bool(passed), detail, expected, observed)
        self.checks.append(c)
        mark = "PASS" if passed else "FAIL"
        log("check", f"{mark} {name}", expected=expected, observed=observed, detail=detail)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)


# ---- independent packet codec ------------------------------------------------
def parse_ipv4(s: str) -> bytes:
    parts = [int(p) for p in s.split(".")]
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        raise ValueError("bad ipv4 " + s)
    return bytes(parts)


def ipv4_str(b: bytes) -> str:
    return ".".join(str(x) for x in b)


def encode(xid: int, msg_type: int, mac: bytes, *, ciaddr=b"\x00" * 4,
           requested: Optional[bytes] = None, server_id: Optional[bytes] = None,
           client_id: Optional[bytes] = None, secs: int = 0, broadcast: bool = False,
           extra_opts: Optional[list] = None) -> bytes:
    buf = bytearray(236)
    buf[0] = OP_REQUEST
    buf[1] = 1  # ethernet
    buf[2] = 6  # hlen
    struct.pack_into(">I", buf, 4, xid & 0xFFFFFFFF)
    struct.pack_into(">H", buf, 8, secs & 0xFFFF)
    if broadcast:
        struct.pack_into(">H", buf, 10, 0x8000)
    buf[12:16] = ciaddr
    buf[28:28 + len(mac)] = mac
    out = bytes(buf) + MAGIC
    opts = bytes([O_MSG_TYPE, 1, msg_type])
    if client_id is not None:
        opts += bytes([O_CLIENT_ID, len(client_id)]) + client_id
    if requested is not None:
        opts += bytes([O_REQ_IP, 4]) + requested
    if server_id is not None:
        opts += bytes([O_SERVER_ID, 4]) + server_id
    if extra_opts:
        for o in extra_opts:
            opts += o
    return out + opts + bytes([255])


def decode(raw: bytes) -> dict:
    if len(raw) < 240:
        raise ValueError("packet too short")
    if raw[236:240] != MAGIC:
        raise ValueError("bad magic cookie")
    d = {
        "op": raw[0], "xid": struct.unpack_from(">I", raw, 4)[0],
        "flags": struct.unpack_from(">H", raw, 10)[0],
        "ciaddr": bytes(raw[12:16]), "yiaddr": bytes(raw[16:20]),
        "siaddr": bytes(raw[20:24]), "chaddr": bytes(raw[28:34]),
        "opts": {},
    }
    i = 240
    while i < len(raw):
        code = raw[i]
        i += 1
        if code == 0:
            continue
        if code == 255:
            break
        ln = raw[i]
        i += 1
        d["opts"].setdefault(code, bytes(raw[i:i + ln]))
        i += ln
    d["msg_type"] = d["opts"].get(O_MSG_TYPE, b"")[0] if O_MSG_TYPE in d["opts"] else None
    return d


# ---- client over raw loopback UDP -------------------------------------------
class DHCPClient:
    def __init__(self, server: str, ident: int, timeout: float = 2.0):
        host, port = server.split(":")
        self.server_addr = (host, int(port))
        self.mac = bytes([0x02, 0, 0, (ident >> 8) & 0xFF, 0, ident & 0xFF])
        self.client_id = bytes([1]) + self.mac
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Bind an ephemeral loopback socket, never a production NIC.
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(timeout)
        self.xid_seq = ident * 1000

    def close(self):
        self.sock.close()

    def next_xid(self) -> int:
        self.xid_seq += 1
        return self.xid_seq & 0xFFFFFFFF

    def send_raw(self, pkt: bytes):
        self.sock.sendto(pkt, self.server_addr)

    def recv(self, timeout: Optional[float] = None) -> Optional[bytes]:
        old = self.sock.gettimeout()
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            data, src = self.sock.recvfrom(2048)
            # Only accept frames from the server address we contacted.
            if src[0] != self.server_addr[0] or src[1] != self.server_addr[1]:
                return None
            return data
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(old)

    def discover(self, xid: Optional[int] = None, secs: int = 0) -> bytes:
        xid = xid if xid is not None else self.next_xid()
        pkt = encode(xid, DISCOVER, self.mac, client_id=self.client_id, secs=secs)
        self.send_raw(pkt)
        return pkt

    def request_select(self, ip: str, xid: Optional[int] = None) -> bytes:
        xid = xid if xid is not None else self.next_xid()
        pkt = encode(xid, REQUEST, self.mac,
                     requested=parse_ipv4(ip), server_id=parse_ipv4("127.0.0.1"),
                     client_id=self.client_id)
        self.send_raw(pkt)
        return pkt

    def request_reboot(self, ip: str) -> bytes:
        pkt = encode(self.next_xid(), REQUEST, self.mac,
                     requested=parse_ipv4(ip), client_id=self.client_id)
        self.send_raw(pkt)
        return pkt

    def request_renew(self, ip: str) -> bytes:
        pkt = encode(self.next_xid(), REQUEST, self.mac,
                     ciaddr=parse_ipv4(ip), client_id=self.client_id)
        self.send_raw(pkt)
        return pkt

    def release(self, ip: str) -> bytes:
        pkt = encode(self.next_xid(), RELEASE, self.mac,
                     ciaddr=parse_ipv4(ip), client_id=self.client_id)
        self.send_raw(pkt)
        return pkt


# ---- HTTP diagnostics helper -------------------------------------------------
class DiagAPI:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def _get(self, path: str):
        req = urllib.request.Request(self.base + path)
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode())

    def post(self, path: str, body: dict):
        data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def version(self) -> dict:
        return self._get("/version")

    def leases(self, state: Optional[str] = None) -> list:
        path = "/api/leases?limit=500"
        if state:
            path += "&state=" + state
        return self._get(path)["leases"]

    def active_lease_for_mac(self, mac: bytes):
        """Return the active (OFFERED/BOUND) row whose label ends with mac hex.

        The server excludes RELEASED/EXPIRED from this filtered view via
        identity membership; we additionally filter client-side by state.
        """
        mac_hex = mac.hex()
        rows = self.leases()
        best = None
        for r in rows:
            if r.get("state") not in ("OFFERED", "BOUND"):
                continue
            label = r.get("identityLabel", "")
            # label format: "cid:<hex client-id>" or "mac:<hex mac>"
            if label.endswith(mac_hex) or ("mac:" + mac_hex) in label:
                best = r
        return best

    def advance(self, duration: str):
        return self.post("/test/clock/advance", {"duration": duration})

    def sweep(self):
        return self.post("/test/sweep", {})

    def reset(self):
        return self.post("/test/reset", {})

    def events(self):
        return self._get("/api/events?limit=500&all=1")["events"]


# ---- scenarios ---------------------------------------------------------------
def assert_offer(raw: bytes, xid: int, pool_lo: str) -> dict:
    d = decode(raw)
    assert d["op"] == OP_REPLY, "expected BOOTREPLY"
    assert d["msg_type"] == OFFER, f"expected OFFER got {d['msg_type']}"
    assert d["xid"] == xid, "xid mismatch in OFFER"
    assert ipv4_str(d["yiaddr"]) != "0.0.0.0", "OFFER yiaddr must be set"
    assert O_SERVER_ID in d["opts"], "OFFER must contain server-id"
    assert O_LEASE in d["opts"], "OFFER must contain lease-time"
    assert ipv4_str(d["opts"][O_SERVER_ID]) == "127.0.0.1"
    return d


def scenario_dora(cl: DHCPClient, rep: Report, pool_lo: str):
    log("dora", "DISCOVER", client=cl.mac.hex())
    xid = cl.next_xid()
    cl.discover(xid)
    raw = cl.recv()
    if raw is None:
        rep.record("dora.offer", False, "no OFFER received", "OFFER reply", "timeout")
        return None
    offer = decode(raw)
    ok = offer["msg_type"] == OFFER and offer["xid"] == xid and ipv4_str(offer["yiaddr"]) == pool_lo
    rep.record("dora.offer", ok,
               f"OFFER yiaddr={ipv4_str(offer['yiaddr'])} xid={offer['xid']} pool-first={pool_lo}",
               f"OFFER xid={xid} yiaddr={pool_lo}",
               f"type={offer['msg_type']} xid={offer['xid']} yiaddr={ipv4_str(offer['yiaddr'])}")
    # OFFER reservation is not a lease: lease option present but commit pending.
    rep.record("dora.offer_has_options", O_SERVER_ID in offer["opts"] and O_LEASE in offer["opts"],
               "server-id and lease-time present in OFFER")

    ip = ipv4_str(offer["yiaddr"])
    log("dora", "REQUEST(selecting)", ip=ip)
    req_raw = cl.request_select(ip, xid=xid + 1)
    ack_raw = cl.recv()
    if ack_raw is None:
        rep.record("dora.ack", False, "no ACK received", "ACK reply", "timeout")
        return None
    ack = decode(ack_raw)
    lease_secs = struct.unpack(">I", ack["opts"][O_LEASE])[0] if O_LEASE in ack["opts"] else 0
    ok = ack["msg_type"] == ACK and ack["xid"] == xid + 1 and ipv4_str(ack["yiaddr"]) == ip and lease_secs > 0
    rep.record("dora.ack", ok, f"ACK yiaddr={ipv4_str(ack['yiaddr'])} lease={lease_secs}s",
               "ACK with positive lease-time", f"type={ack['msg_type']} lease={lease_secs}")

    # Duplicate REQUEST byte retransmission: identical ACK, no extension.
    before = cl.recv(0.05)  # drain
    cl.send_raw(req_raw)
    replay_raw = cl.recv()
    same = replay_raw == ack_raw
    rep.record("dora.duplicate_request_byte_identical", same,
               "retransmitted REQUEST replays byte-identical ACK",
               f"{len(ack_raw)} bytes identical", f"{len(replay_raw) if replay_raw else 0} bytes")
    return ip


def scenario_duplicate_no_extend(cl: DHCPClient, rep: Report, diag: DiagAPI, ip: str):
    # Find the bound lease before, retransmit DISCOVER, ensure ends unchanged.
    before = diag.active_lease_for_mac(cl.mac)
    if not before or before.get("state") != "BOUND":
        rep.record("dup.lease_bound_before", False, f"no BOUND lease before replay: {before}")
        return
    ends_before = before["ends"]
    disc = cl.discover()
    off_raw = cl.recv()
    if off_raw is None:
        rep.record("dup.discover_reply", False, "bound client re-DISCOVER got no OFFER")
        return
    offer = decode(off_raw)
    rep.record("dup.bound_client_rediscovers_same_ip",
               offer["msg_type"] == OFFER and ipv4_str(offer["yiaddr"]) == ip,
               f"re-offer ip={ipv4_str(offer['yiaddr'])}")
    after = diag.active_lease_for_mac(cl.mac)
    unchanged = after and after["state"] == "BOUND" and after["ends"] == ends_before
    rep.record("dup.lease_ends_unchanged", bool(unchanged),
               f"re-DISCOVER did not extend lease ends: {ends_before} -> {after.get('ends') if after else None}")


def scenario_old_xid_replay(cl: DHCPClient, rep: Report):
    # An ancient xid (wrapping far from current sequence) asking for an address
    # INSIDE the pool that this client was never offered must produce an
    # explicit NAK (nak_no_valid_offer), not a silent success.
    old_xid = 0xDEADBEEF
    target = "127.50.0.55"
    pkt = encode(old_xid, REQUEST, cl.mac,
                 requested=parse_ipv4(target),
                 server_id=parse_ipv4("127.0.0.1"), client_id=cl.client_id)
    cl.send_raw(pkt)
    raw = cl.recv()
    if raw is None:
        rep.record("oldxid.nak", False, "no reply to stale REQUEST", "NAK", "timeout")
        return
    d = decode(raw)
    msg = d["opts"].get(O_MESSAGE, b"").decode(errors="replace")
    ok = d["msg_type"] == NAK and d["xid"] == old_xid and msg == "nak_no_valid_offer"
    rep.record("oldxid.nak", ok,
               f"NAK reason={msg} xid={d['xid']}",
               "NAK nak_no_valid_offer with mirrored xid",
               f"type={d['msg_type']} reason={msg}")


def scenario_init_reboot(cl: DHCPClient, rep: Report, diag: DiagAPI, ip: str):
    before = diag.active_lease_for_mac(cl.mac)
    cl.request_reboot(ip)
    raw = cl.recv()
    if raw is None:
        rep.record("reboot.ack", False, "no reply to INIT-REBOOT")
        return
    d = decode(raw)
    after = diag.active_lease_for_mac(cl.mac)
    ok = (d["msg_type"] == ACK and ipv4_str(d["yiaddr"]) == ip and
          before and after and before["ends"] == after["ends"])
    rep.record("reboot.confirms_without_extension", ok,
               f"reboot ACK ip={ipv4_str(d['yiaddr'])} ends {before['ends'] if before else '?'} -> {after['ends'] if after else '?'}",
               "ACK with unchanged lease boundaries",
               f"type={d['msg_type']} same_ends={bool(before and after and before['ends'] == after['ends'])}")


def scenario_expiry(cl: DHCPClient, rep: Report, diag: DiagAPI, ip: str, server: str):
    # Advance beyond the lease. The row is still BOUND (sweeper has not run),
    # so a renew must be NAKed with nak_lease_expired; then the sweep moves it
    # to EXPIRED and the address becomes reallocatable.
    code, body = diag.advance("30s")
    rep.record("expiry.clock_advance", code == 200 and body.get("ok") is True,
               f"fake clock advance http={code} body={body}")

    cl.request_renew(ip)
    raw = cl.recv()
    if raw is None:
        rep.record("expiry.stale_renew_nak", False, "expected NAK for expired renew, got silence")
    else:
        d = decode(raw)
        reason = d["opts"].get(O_MESSAGE, b"").decode(errors="replace")
        rep.record("expiry.stale_renew_nak", d["msg_type"] == NAK and reason == "nak_lease_expired",
                   f"NAK reason={reason}", "NAK nak_lease_expired", f"type={d['msg_type']} reason={reason}")

    code, body = diag.sweep()
    changed = body.get("changes", []) if code == 200 else []
    moved = any(c.get("ip") == ip and c.get("to") == "EXPIRED" and c.get("from") == "BOUND"
                for c in changed)
    rep.record("expiry.sweep_to_expired", moved, f"sweep changes={changed}",
               f"a BOUND->EXPIRED change for {ip}", str(changed))

    # After expiry the same address must be re-offerable to a new client.
    newcomer = DHCPClient(server, 0x800)
    try:
        xid = newcomer.next_xid()
        newcomer.discover(xid)
        off = newcomer.recv()
        ok = off is not None and decode(off)["yiaddr"] == parse_ipv4(ip)
        rep.record("expiry.address_reallocatable", bool(ok),
                   f"new client offered freed ip: {off is not None}",
                   f"OFFER yiaddr={ip}", "")
    finally:
        newcomer.close()


def scenario_contention(rep: Report, server: str, diag: DiagAPI, n: int):
    # n clients race DISCOVER+REQUEST through real UDP sockets concurrently.
    results = {}
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker(i: int):
        c = DHCPClient(server, 0x600 + i, timeout=3.0)
        try:
            barrier.wait()
            xid = c.next_xid()
            c.discover(xid)
            off_raw = c.recv()
            if off_raw is None:
                with lock:
                    results[i] = ("no_offer", None)
                return
            offer = decode(off_raw)
            if offer["msg_type"] != OFFER:
                with lock:
                    results[i] = ("not_offer", offer["msg_type"])
                return
            ip = ipv4_str(offer["yiaddr"])
            c.request_select(ip)
            ack_raw = c.recv()
            if ack_raw is None:
                with lock:
                    results[i] = ("no_ack", ip)
                return
            ack = decode(ack_raw)
            with lock:
                results[i] = ("ack" if ack["msg_type"] == ACK else f"nak/{ack['msg_type']}", ip)
        except Exception as e:  # report, never mask as success
            with lock:
                results[i] = (f"exception:{e}", None)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    owners = {}
    ack_count = 0
    bad = []
    for i, (status, ip) in sorted(results.items()):
        if status == "ack":
            ack_count += 1
            owners.setdefault(ip, []).append(i)
        else:
            bad.append((i, status))
    unique = len(owners) == ack_count and all(len(v) == 1 for v in owners.values())
    rep.record("contention.all_successful", ack_count == n and not bad,
               f"acks={ack_count}/{n} bad={bad}", f"all {n} clients ACK", str(results))
    rep.record("contention.unique_owner_per_ip", unique,
               f"ip->clients={owners}", "each IP owned by exactly one client", str(owners))

    # Cross-check persisted state: exactly n distinct BOUND rows.
    rows = diag.leases()
    bound = [r for r in rows if r.get("state") == "BOUND"]
    distinct = len({r["ip"] for r in bound})
    rep.record("contention.persisted_bound", len(bound) == n and distinct == n,
               f"BOUND rows={len(bound)} distinctIP={distinct}")


def scenario_release_ownership(rep: Report, server: str, diag: DiagAPI, ip: str):
    owner = DHCPClient(server, 0x701)
    stranger = DHCPClient(server, 0x702)
    try:
        # owner is already bound to ip from DORA scenario (identity must match
        # the DORA client MAC; instead we set up a fresh two-client exchange on
        # a fresh pool address by driving one full DORA for owner).
        xid = owner.next_xid()
        owner.discover(xid)
        raw = owner.recv()
        if raw is None:
            rep.record("release.owner_offer", False, "no offer for owner")
            return
        offer = decode(raw)
        own_ip = ipv4_str(offer["yiaddr"])
        owner.request_select(own_ip)
        ack_raw = owner.recv()
        if ack_raw is None or decode(ack_raw)["msg_type"] != ACK:
            rep.record("release.owner_ack", False, "owner not bound")
            return

        stranger.release(own_ip)
        # RELEASE never yields a reply; verify state remained BOUND instead.
        silent = stranger.recv(0.4) is None
        row = diag.active_lease_for_mac(owner.mac)
        untouched = row is not None and row.get("state") == "BOUND"
        rep.record("release.stranger_blocked", silent and untouched,
                   f"stranger release: silent={silent} owner lease={row.get('state') if row else None}",
                   "no reply and owner lease stays BOUND", "")

        owner.release(own_ip)
        silent = owner.recv(0.4) is None
        row = diag.active_lease_for_mac(owner.mac)
        gone = row is None or row.get("state") in ("RELEASED", "EXPIRED")
        rep.record("release.owner_frees_address", silent and gone,
                   f"owner release silent={silent} latestRow={row}",
                   "no reply and address released", f"row={row}")

        # Duplicate RELEASE is idempotent: no reply, no resurrection.
        owner.release(own_ip)
        again_silent = owner.recv(0.4) is None
        row2 = diag.active_lease_for_mac(owner.mac)
        rep.record("release.duplicate_idempotent", again_silent and (row2 == row),
                   "duplicate RELEASE causes no state change or reply")
    finally:
        owner.close()
        stranger.close()


def scenario_malformed(cl: DHCPClient, rep: Report, server: str):
    s = cl.sock
    # 1. Truncated garbage
    s.sendto(b"\x01\x02\x03", cl.server_addr)
    r1 = cl.recv(0.4)
    rep.record("malformed.truncated_no_reply", r1 is None,
               "garbage datagram must not produce a reply", "silence",
               f"{len(r1) if r1 else 0} bytes")

    # 2. Well-formed but unsupported message type (DECLINE/INFORM)
    pkt = encode(cl.next_xid(), DECLINE, cl.mac, client_id=cl.client_id)
    s.sendto(pkt, cl.server_addr)
    r2 = cl.recv(0.4)
    rep.record("malformed.unsupported_no_success", r2 is None,
               "DECLINE outside subset must not return a success-looking reply",
               "silence / explicit reject", f"{len(r2) if r2 else 0} bytes")

    # 3. Bad magic cookie
    bad = bytearray(encode(cl.next_xid(), DISCOVER, cl.mac, client_id=cl.client_id))
    bad[236] ^= 0xFF
    s.sendto(bytes(bad), cl.server_addr)
    r3 = cl.recv(0.4)
    rep.record("malformed.bad_cookie_no_reply", r3 is None,
               "bad magic cookie must be rejected silently")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--udp", default="127.0.0.1:10067")
    ap.add_argument("--http", default="http://127.0.0.1:18080")
    ap.add_argument("--run-id", default="oracle-" + str(int(time.time())))
    ap.add_argument("--report", default="oracle_report.json")
    ap.add_argument("--pool-lo", default="127.50.0.2")
    ap.add_argument("--contention", type=int, default=4,
                    help="number of parallel contenders; pool must have >= N free IPs")
    ap.add_argument("--no-reset", action="store_true",
                    help="do not call POST /test/reset before scenarios")
    args = ap.parse_args()

    rep = Report(run_id=args.run_id)
    log("start", "independent DHCPv4 oracle", run_id=args.run_id, udp=args.udp, http=args.http)

    diag = DiagAPI(args.http)
    try:
        v = diag.version()
        rep.version = v.get("banner", "")
        log("version", "server", banner=v.get("banner"), version=v.get("version"))
    except Exception as e:
        print(f"FATAL: cannot reach diagnostics API {args.http}: {e}", file=sys.stderr)
        print("Start the server first: scripts/start_test_server.sh", file=sys.stderr)
        return 2

    if not args.no_reset:
        code, body = diag.reset()
        if code != 200:
            print(f"FATAL: /test/reset failed http={code} body={body}", file=sys.stderr)
            return 2
        log("reset", "state and fake clock reset", now=body.get("now"))

    main_cl = DHCPClient(args.udp, 0x101)
    malformed_cl = DHCPClient(args.udp, 0x999)
    try:
        ip = scenario_dora(main_cl, rep, args.pool_lo)
        if ip:
            scenario_duplicate_no_extend(main_cl, rep, diag, ip)
            scenario_old_xid_replay(main_cl, rep)
            scenario_init_reboot(main_cl, rep, diag, ip)
            scenario_expiry(main_cl, rep, diag, ip, args.udp)
        else:
            rep.record("dora.prerequisite", False, "DORA did not complete; dependent scenarios skipped",
                       "OFFER+ACK", "failed")
        scenario_contention(rep, args.udp, diag, args.contention)
        scenario_release_ownership(rep, args.udp, diag, args.pool_lo)
        scenario_malformed(malformed_cl, rep, args.udp)
    finally:
        main_cl.close()
        malformed_cl.close()

    # Events must be correlatable to the SERVER run identity and to each
    # request's xid, with an explicit result/reason/action triple (abnormal
    # outcomes are never folded into success). The UDP wire format carries no
    # run-id field, so server events are tagged with the daemon run id; the
    # oracle's own run id tags only the requests it injects over HTTP.
    try:
        health = diag._get("/healthz")
        server_run = health.get("runId", "")
        events = diag.events()
        with_run = [e for e in events if str(e.get("runId")) == server_run]
        has_structure = all(
            ("result" in e and "reason" in e and "action" in e and "xid" in e
             and e.get("result") in ("ok", "nak", "no_reply", "malformed_packet",
                                     "unsupported_message", "pool_exhausted",
                                     "address_contention", "internal_error"))
            for e in events)
        non_success = [e for e in events if e.get("result") != "ok"]
        ok = len(events) > 0 and len(with_run) == len(events) and has_structure and len(non_success) > 0
        rep.record("events.run_correlation", ok,
                   f"events={len(events)} all tagged serverRun={server_run} "
                   f"non_ok={len(non_success)} (e.g. {non_success[0]['reason'] if non_success else '-'})",
                   "every event tagged with the daemon run id, xid and an explicit category",
                   f"serverRunId={server_run}")
    except Exception as e:
        rep.record("events.run_correlation", False, f"events API failed: {e}")

    summary = {
        "runId": rep.run_id, "version": rep.version,
        "total": len(rep.checks),
        "passed": sum(1 for c in rep.checks if c.passed),
        "failed": sum(1 for c in rep.checks if not c.passed),
        "checks": [c.__dict__ for c in rep.checks],
    }
    with open(args.report, "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: summary[k] for k in ("runId", "version", "total", "passed", "failed")}, indent=2))
    print(f"full report: {os.path.abspath(args.report)}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
