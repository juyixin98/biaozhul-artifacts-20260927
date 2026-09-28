"""Example HTTP client walk-through.

Assumes the API is running (see scripts/run_server.sh):

    .venv/bin/python examples/http_client_example.py

It performs a REAL network round trip for every step:
  1. GET  /health
  2. POST /v1/validators/bootstrap for bob + alice
  3. POST /v1/validators/alice/weight (weight change at epoch 2)
  4. POST two conflicting same-target votes for bob -> double_vote evidence
  5. POST the identical vote twice -> duplicate_retransmit, not slashable
  6. GET  evidence list; POST .../recheck (independent verification)
  7. POST /v1/replay (offline consistency check)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from localffg.crypto import Signer

BASE = "http://127.0.0.1:8765"
CHAIN = "local-chain-0"
DOMAIN = b"localffg/validator-vote/v1"


def show(title: str, resp: httpx.Response) -> dict:
    body = resp.json()
    print(f"\n### {title}\nHTTP {resp.status_code}")
    print(json.dumps(body, indent=2, sort_keys=True)[:2400])
    return body


def main() -> int:
    c = httpx.Client(base_url=BASE, timeout=10)

    show("1) health", c.get("/health"))

    # 2) bootstrap synthetic validators (deterministic seeds -> stable pubkeys)
    bob = Signer.from_seed("bob", b"localffg-fixture/bob")
    alice = Signer.from_seed("alice", b"localffg-fixture/alice")
    for vid, key, weight in (("bob", bob, 100), ("alice", alice, 100)):
        r = c.post("/v1/validators/bootstrap",
                   json={"validator_id": vid, "weight": weight, "seed": f"localffg-fixture/{vid}"})
        if r.status_code not in (200, 409):
            show(f"bootstrap {vid} FAILED", r)
            return 1
        show(f"2) bootstrap {vid}", r)

    # 3) weight change for alice at epoch 2
    show("3) alice weight 150 @epoch2",
         c.post("/v1/validators/alice/weight", json={"effective_epoch": 2, "weight": 150}))

    # 4) double vote by bob (same target 8, different source+root)
    v1 = bob.sign_vote(domain=DOMAIN, chain_id=CHAIN, source_round=0, target_round=8,
                       block_root=b"\x01" * 32).to_json_dict()
    v2 = bob.sign_vote(domain=DOMAIN, chain_id=CHAIN, source_round=2, target_round=8,
                       block_root=b"\x02" * 32).to_json_dict()
    show("4a) bob vote 0->8", c.post("/v1/votes", json={**v1, "run_id": "example-run"}))
    body = show("4b) bob vote 2->8 (double vote)", c.post("/v1/votes", json=v2))
    assert body["category"] == "double_vote" and body["slashable"] is True
    evidence_id = body["evidence"][0]["evidence_id"]
    assert body["evidence"][0]["weight_epoch"] == 0 and body["evidence"][0]["weight"] == 100

    # 5) identical retransmission is NOT an offense
    show("5a) retransmit vote 1", c.post("/v1/votes", json=v1))
    again = show("5b) retransmit vote 1 again", c.post("/v1/votes", json=v1))
    assert again["category"] == "duplicate_retransmit" and again["slashable"] is False

    # 6) list + independently re-check the evidence
    show("6a) evidence list", c.get("/v1/evidence"))
    chk = show("6b) independent recheck", c.post(f"/v1/evidence/{evidence_id}/recheck"))
    assert chk["verdict"] == "valid", "independent checker must validate genuine evidence"
    assert chk["derived_kind"] == "double_vote"

    # 7) offline replay consistency
    replay = show("7) offline replay", c.post("/v1/replay"))
    assert replay["verdict"] == "OK", replay["errors"]

    # 8) tampered signature must be rejected with exact category
    bad = dict(v1)
    sig = bytearray(bytes.fromhex(bad["signature"]))
    sig[0] ^= 0xFF
    bad["signature"] = sig.hex()
    tampered = show("8) tampered signature -> 422 invalid_signature", c.post("/v1/votes", json=bad))
    assert tampered["category"] == "invalid_signature"

    print("\nALL EXAMPLE STEPS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
