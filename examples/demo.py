#!/usr/bin/env python3
"""End-to-end curl-style demonstration against the three running services.

It uses only HTTP (stdlib urllib), exactly like an external operator with
curl would, and prints the decision ledger at the end.
"""
import json
import sys
import time
import urllib.request

B = "http://127.0.0.1:18081"
A = "http://127.0.0.1:18082"
D = "http://127.0.0.1:18083"


def call(method, url, body=None, headers=None):
    data = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            if not raw:
                return r.status, {}
            parsed = json.loads(raw)
            return r.status, parsed if parsed is not None else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw.decode()}


def wait(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


print("== create ==")
st, obj = call("POST", B + "/api/v1/namespaces/demo/resources",
               {"name": "widget-demo",
                "spec": {"replicas": 2, "secret": "topsecret"}})
assert st == 201, st
uid = obj["uid"]
print("uid           :", uid)
print("specHash set  :", obj["specHash"][:16], "...")

print("\n== wait convergence to generation 1 ==")
def state():
    _, o = call("GET", B + "/api/v1/namespaces/demo/resources/widget-demo")
    return o
assert wait(lambda: state()["status"].get("observedGeneration") == 1
            and state()["status"].get("externalID"))
o = state()
print("finalizers    :", o["finalizers"])
print("observedGen   :", o["status"]["observedGeneration"])
print("externalID    :", o["status"]["externalID"])

print("\n-- unauthenticated read redacts secret --")
print("spec.secret   :", o["spec"]["secret"])
st, priv = call("GET", B + "/api/v1/namespaces/demo/resources/widget-demo",
                headers={"X-Controller-Auth": "local-dev-secret"})
print("with cred     :", priv["spec"]["secret"])

print("\n== spec update replicas 2 -> 5 ==")
rv = o["resourceVersion"]
st, up = call("PUT", B + "/api/v1/namespaces/demo/resources/widget-demo",
              {"spec": {"replicas": 5, "secret": "topsecret"}},
              {"If-Match": str(rv)})
assert st == 200, (st, up)
print("new generation:", up["generation"], "new rv:", up["resourceVersion"])
assert wait(lambda: state()["status"].get("observedGeneration") == 2)
_, rows = call("GET", A + "/admin/resources")
phys = [r for r in rows if r["ownerUID"] == uid][0]
print("physical      : gen=%s version=%s replicas=%s" %
      (phys["generation"], phys["version"], phys["spec"]["replicas"]))

print("\n== stale If-Match rejected with 409 (new spec preserved) ==")
st, body = call("PUT", B + "/api/v1/namespaces/demo/resources/widget-demo",
                {"spec": {"replicas": 99}}, {"If-Match": str(rv)})
print("status        :", st, "reason:", body.get("reason"))
assert st == 409
assert state()["generation"] == 2

print("\n== fault: external delete fails -> terminating, row kept ==")
st, _ = call("POST", A + "/admin/faults",
             {"ownerUID": uid, "fault": "delete-failed"})
assert st == 200
st, d1 = call("DELETE", B + "/api/v1/namespaces/demo/resources/widget-demo")
print("DELETE status :", st, "terminating:", d1.get("deletionTimestamp") is not None,
      "finalizers:", d1["finalizers"])
time.sleep(0.8)
_, rows = call("GET", A + "/admin/resources")
print("physical rows :", len([r for r in rows if r["ownerUID"] == uid]))
assert len([r for r in rows if r["ownerUID"] == uid]) == 1

print("\n== heal fault -> external cleanup, finalizer off, record purged ==")
st, _ = call("DELETE", A + "/admin/faults/" + uid)
assert st == 204

def purged():
    s, _ = call("GET", B + "/api/v1/namespaces/demo/resources/widget-demo")
    return s == 404
assert wait(purged, 6.0)
_, rows = call("GET", A + "/admin/resources")
print("GET status    : 404")
print("physical rows :", len([r for r in rows if r["ownerUID"] == uid]))
assert len([r for r in rows if r["ownerUID"] == uid]) == 0

print("\n== controller decision ledger ==")
_, ledger = call("GET", D + "/diagnostics/ledger/" + uid + "?limit=2000")
for e in ledger:
    print("  %-10s %-24s cat=%-16s req=%s" %
          (e["phase"], e["decision"], e.get("category") or "-",
           e["requestID"][:20]))
    print("      detail:", e["detail"][:90])

print("\nDEMO OK")
