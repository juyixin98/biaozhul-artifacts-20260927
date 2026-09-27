#!/usr/bin/env python3
"""
End-to-end HTTP smoke check for prereg2d-server.

All inputs are deterministic and all expected answers were hand-computed
(fixtures/handcalc.json mirrors them). The script asserts exact sums,
status codes and error_code categories; on failure it prints the run id,
the failing step, the request/response pair, and exits non-zero.

Usage: smoke_http.py <base_url> <run_id>
"""
import json
import sys
import urllib.error
import urllib.request

BASE, RUN = sys.argv[1], sys.argv[2]
failures = []
step_no = 0


def step(name):
    global step_no
    step_no += 1
    print(f"[{RUN}] step {step_no:02d}: {name}", flush=True)


def call(method, path, body=None, rid=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    sent_rid = rid or f"{RUN}-s{step_no}"
    req.add_header("x-request-id", sent_rid)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read()), sent_rid
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), sent_rid


def expect(name, cond, detail):
    if cond:
        print(f"    PASS: {name}")
    else:
        print(f"    FAIL: {name} :: {detail}")
        failures.append((name, detail))


# 1. health on an empty store
step("health before initialization")
st, v, _ = call("GET", "/health")
expect("health 200", st == 200, st)
expect("head_version null", v.get("head_version") is None, v)

# 2. query/init ordering: batch before register -> 412 not_initialized
step("batch before registration is rejected")
st, v, rid = call("POST", "/batches", {"updates": []})
expect("status 412", st == 412, (st, v))
expect("code not_initialized", v.get("error_code") == "not_initialized", v)
expect("request id echoed", v.get("request_id") == rid, v)

# 3. register
step("register coordinates")
st, v, _ = call("POST", "/admin/register", {"xs": [-5, 0, 7], "ys": [-3, 2, 100]})
expect("register 200", st == 200, (st, v))
expect("v1", v.get("version") == 1 and v.get("kind") == "registered", v)

# 4. baseline batch with repeated coord inside batch -> reject
step("duplicate coordinate inside one batch rejected")
st, v, _ = call("POST", "/batches",
                {"updates": [{"x": 0, "y": 2, "delta": 1},
                             {"x": 0, "y": 2, "delta": 2}]})
expect("status 422", st == 422, (st, v))
expect("code duplicate_in_batch", v.get("error_code") == "duplicate_in_batch", v)

# 5. unregistered coordinate -> reject
step("unregistered coordinate rejected")
st, v, _ = call("POST", "/batches", {"updates": [{"x": 1, "y": 2, "delta": 4}]})
expect("status 422", st == 422, (st, v))
expect("code unregistered_coordinate", v.get("error_code") == "unregistered_coordinate", v)

# 6. real baseline batch (v2): (-5,-3)=3, (0,2)=-5, (7,100)=10
step("baseline batch (v2)")
st, v, _ = call("POST", "/batches",
                {"updates": [{"x": -5, "y": -3, "delta": 3},
                             {"x": 0, "y": 2, "delta": -5},
                             {"x": 7, "y": 100, "delta": 10}]})
expect("v2", st == 200 and v.get("version") == 2, (st, v))

# 7. exact hand-computed rectangle sums
checks = [
    ("whole plane", "&x_lo=-9223372036854775808&x_hi=9223372036854775807"
                    "&y_lo=-9223372036854775808&y_hi=9223372036854775807", 8),
    ("mixed-sign window", "&x_lo=-5&x_hi=0&y_lo=-3&y_hi=2", -2),
    ("single negative point", "&x_lo=0&x_hi=0&y_lo=2&y_hi=2", -5),
    ("empty gap", "&x_lo=1&x_hi=6&y_lo=-3&y_hi=100", 0),
    ("inverted x empty", "&x_lo=1&x_hi=0&y_lo=-3&y_hi=100", 0),
    ("boundary inclusive", "&x_lo=-5&x_hi=7&y_lo=-3&y_hi=100", 8),
]
for name, qs, want in checks:
    step(f"query: {name}")
    path = f"/query?version=2&{qs}"
    st, v, _ = call("GET", path)
    expect("200", st == 200, (st, v))
    expect(f"sum={want}", v.get("sum") == want, v)
    expect("explain present", isinstance(v.get("explain", {}).get("terms"), dict), v)

# 8. i128 extreme bounds
step("extreme i128 bounds accepted")
st, v, _ = call("GET", "/query?x_lo=-170141183460469231731687303715884105728"
                       "&x_hi=170141183460469231731687303715884105727"
                       "&y_lo=-170141183460469231731687303715884105728"
                       "&y_hi=170141183460469231731687303715884105727")
expect("whole plane via i128 bounds", st == 200 and v.get("sum") == 8, (st, v))

# 9. overflow (10 + i64::MAX) -> rejected, no new version
step("accumulation overflow rejected")
st, v, _ = call("POST", "/batches",
                {"updates": [{"x": 7, "y": 100, "delta": 9223372036854775807}]})
expect("422 overflow", st == 422 and v.get("error_code") == "overflow", (st, v))

# 10. malformed JSON
step("malformed JSON categorized")
# send raw bad body via a dedicated call
req = urllib.request.Request(BASE + "/batches", data=b"{nope", method="POST")
req.add_header("content-type", "application/json")
req.add_header("x-request-id", f"{RUN}-badjson")
try:
    with urllib.request.urlopen(req, timeout=5) as r:
        st, body = r.status, json.loads(r.read())
except urllib.error.HTTPError as e:
    st, body = e.code, json.loads(e.read())
expect("422 bad_request", st == 422 and body.get("error_code") == "bad_request", (st, body))
expect("correlation id", body.get("request_id") == f"{RUN}-badjson", body)

# 11. rebuild: drop (-5,-3); carry -5 and +10
step("rebuild coordinate tables -> v3")
st, v, _ = call("POST", "/admin/rebuild", {"xs": [0, 7, 42], "ys": [2, 100]})
expect("v3 rebuild", st == 200 and v.get("version") == 3 and v.get("kind") == "rebuild", (st, v))

# 12. old v2 still returns 8, head v3 returns 5
step("old version still queryable after rebuild")
st, v, _ = call("GET", "/query?version=2&x_lo=-9223372036854775808&x_hi=9223372036854775807&y_lo=-9223372036854775808&y_hi=9223372036854775807")
expect("v2 sum still 8", st == 200 and v.get("sum") == 8, (st, v))
st, v, _ = call("GET", "/query?version=3&x_lo=-9223372036854775808&x_hi=9223372036854775807&y_lo=-9223372036854775808&y_hi=9223372036854775807")
expect("v3 head sum 5", st == 200 and v.get("sum") == 5, (st, v))

# 13. unknown version -> 404
step("unknown version -> 404")
st, v, _ = call("GET", "/query?version=99&x_lo=0&x_hi=0&y_lo=0&y_hi=0")
expect("404 unknown_version", st == 404 and v.get("error_code") == "unknown_version", (st, v))

if failures:
    print(f"\n[{RUN}] SMOKE FAILED: {len(failures)} failed assertion(s)")
    for name, detail in failures:
        print(f"  - {name}: {detail}")
    sys.exit(1)
print(f"\n[{RUN}] SMOKE PASSED: {step_no} steps, all assertions held")
