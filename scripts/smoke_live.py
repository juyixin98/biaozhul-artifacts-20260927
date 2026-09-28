#!/usr/bin/env python3
"""End-to-end smoke check against a running uvicorn instance.

Exercises the real HTTP stack (no in-process shortcuts): whole-document
redaction, cross-chunk streaming equality, escaped JSON, adjacent secrets,
rule switching, uncertain fragments, audit authorization and the
explainability fields. Exits non-zero on the first failed expectation and
prints the named failure reason.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8088"
AUDIT_KEY = "local-synthetic-audit-key"
FAILED: list[str] = []


def call(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode()), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode()), dict(exc.headers)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILED.append(name)


# Synthetic secrets generated with valid check digits.
from app.rules import luhn_ok, cn_id_ok  # noqa: E402
from tests.synth_fixtures import (  # noqa: E402
    SYNTH_API_TOKEN, SYNTH_BANK_CARD, SYNTH_CN_ID, SYNTH_EMAIL,
    BAD_BANK_CARD, BAD_CN_ID,
)


def main() -> int:
    assert luhn_ok(SYNTH_BANK_CARD) and cn_id_ok(SYNTH_CN_ID)
    print("[1] health + rule versions")
    st, body, _ = call("GET", "/health")
    check("health ok", st == 200 and body["status"] == "ok", str(body))
    check("audit chain intact", body["audit_chain"]["ok"] is True)

    print("[2] whole-document redaction + exact mapping")
    text = f"mail {SYNTH_EMAIL} and token {SYNTH_API_TOKEN}"
    st, body, _ = call("POST", "/api/v1/redact", {"text": text})
    check("redact status 200", st == 200, str(body))
    check(
        "exact redacted text",
        body["redacted"] == "mail [REDACTED:email] and token [REDACTED:api_token]",
        body.get("redacted"),
    )
    check("request id present", body["request_id"].startswith("req_"))
    check("version/fingerprint explained", body["rule_version"] == "standard-v1"
          and len(body["rule_fingerprint"]) == 16)
    check("offset map complete", len(body["mappings"]) == 2
          and body["mappings"][0]["original_start"] == 5)
    check("no residual secret", SYNTH_EMAIL not in body["redacted"]
          and SYNTH_API_TOKEN not in body["redacted"])

    print("[3] streaming across arbitrary 3-char chunks == whole result")
    st, opened, _ = call("POST", "/api/v1/sessions", {"text": ""})
    sid = opened["session_id"]
    emitted = ""
    for i in range(0, len(text), 3):
        final = i + 3 >= len(text)
        st, ch, _ = call("POST", "/api/v1/sessions/chunk",
                         {"session_id": sid, "chunk": text[i:i + 3], "final": final})
        check("chunk 200", st == 200, str(ch)) if i == 0 else None
        emitted += ch["emitted"]
    check("stream == full", emitted == body["redacted"], emitted)

    print("[4] escaped JSON survives as parseable JSON, secret gone")
    inner = json.dumps({"email": SYNTH_EMAIL, "n": 1})
    envelope = json.dumps({"payload": inner})
    st, body4, _ = call("POST", "/api/v1/redact", {"text": envelope})
    parsed = json.loads(json.loads(body4["redacted"])["payload"])
    check("escaped value redacted", parsed["email"] == "[REDACTED:field]")

    print("[5] adjacent + overlapping rules")
    st, body5, _ = call("POST", "/api/v1/redact",
                        {"text": f'"token":"{SYNTH_API_TOKEN}"{SYNTH_EMAIL}'})
    check("field priority + adjacency",
          body5["redacted"] == '"token":"[REDACTED:field]"[REDACTED:email]',
          body5["redacted"])

    print("[6] rule switching (strict adds validated CN-ID rule)")
    st, std, _ = call("POST", "/api/v1/redact", {"text": SYNTH_CN_ID})
    st, strict, _ = call("POST", "/api/v1/redact",
                         {"text": SYNTH_CN_ID, "profile": "strict"})
    check("standard leaves synthetic id", std["redacted"] == SYNTH_CN_ID)
    check("strict redacts it", strict["redacted"] == "[REDACTED:cn_id_card]")

    print("[7] uncertain evidence is listed, not silently released/redacted")
    st, unc, _ = call("POST", "/api/v1/redact",
                      {"text": f"bad {BAD_BANK_CARD}", "profile": "strict"})
    check("bad checksums not redacted confidently", unc["mappings"] == [])
    labels = {(u["label"], u["reason"]) for u in unc["uncertain"]}
    check("uncertain named", ("bank_card?", "evidence_check_failed:luhn") in labels
          or ("cn_id_card?", "evidence_check_failed:cn_id_checksum") in labels,
          str(labels))

    print("[8] audit interface authorization + reveal + explainability")
    st, denied, _ = call("GET", "/api/v1/audit/requests")
    check("audit denied without key", st == 403
          and denied["error_category"] == "audit_access_denied")
    st, listing, _ = call("GET", "/api/v1/audit/requests",
                          headers={"X-Audit-Key": AUDIT_KEY})
    check("audit listed with key", st == 200 and len(listing["requests"]) >= 1)
    rid = body["request_id"]
    st, detail, _ = call("GET", f"/api/v1/audit/requests/{rid}?reveal=true",
                         headers={"X-Audit-Key": AUDIT_KEY})
    originals = {f["original"] for f in detail["fragments"]}
    check("revealed originals match", originals >= {SYNTH_EMAIL, SYNTH_API_TOKEN})
    check("request meta explains version",
          detail["request"]["rule_version"] == "standard-v1")
    st, chain, _ = call("GET", "/api/v1/audit/chain",
                        headers={"X-Audit-Key": AUDIT_KEY})
    check("hash chain verifies", chain["chain"]["ok"] is True)

    print("[9] mid-stream profile conflict is a named failure")
    st, op, _ = call("POST", "/api/v1/sessions", {"text": "", "profile": "standard"})
    st, conflict, _ = call("POST", "/api/v1/sessions/chunk",
                           {"session_id": op["session_id"], "chunk": "x",
                            "profile": "strict"})
    check("409 profile_conflict", st == 409
          and conflict["error_category"] == "profile_conflict")

    print()
    if FAILED:
        print(f"SMOKE RESULT: FAIL ({len(FAILED)} failed): {FAILED}")
        return 1
    print("SMOKE RESULT: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
