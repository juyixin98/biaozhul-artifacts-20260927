#!/usr/bin/env python3
"""Local HTTP service demo.

Starts the FastAPI app in-process (TestClient, no external network) and
exercises the real HTTP boundary: bootstrap, valid header accept, weighted
rejection (409), trust-period rejection and the audit endpoint.

For a real listening server use:
    LC_DB_PATH=./data/lc.db \\
    LC_CHECKPOINT_KEY_HEX=<trusted checkpoint public key hex> \\
    .venv/bin/python -m lightclient.run
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from lightclient import codec
from lightclient.config import LightClientConfig
from lightclient.fixtures.builder import ChainBuilder
from lightclient.service import create_app


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lc-http-"))
    builder = ChainBuilder()
    _g, env = builder.genesis(timestamp=1_000_000)
    app = create_app(
        store_path=str(tmp / "svc.db"),
        config=LightClientConfig(),
        trusted_checkpoint_key=builder.checkpoint_pub,
        run_id="http-demo",
    )
    failures = []

    with TestClient(app) as c:
        def show(label, resp, expect_status, expect=None):
            body = resp.json()
            ok = resp.status_code == expect_status
            if expect:
                ok = ok and expect(body)
            print(f"  [{'OK  ' if ok else 'FAIL'}] {label}: HTTP {resp.status_code}")
            if not ok:
                failures.append(label)
            if body.get("error"):
                err = body["error"]
                print(
                    f"         error={err['code']} category={err['category']} "
                    f"detail={err['detail']}"
                )
            return body

        print("== health (pre-bootstrap) ==")
        show("GET /health", c.get("/health"), 200,
             lambda b: b["initialized"] is False)

        print("== bootstrap ==")
        show(
            "POST /bootstrap",
            c.post("/bootstrap",
                   json={"checkpoint_envelope": codec.encode_envelope(env).hex()}),
            200, lambda b: b["tip"]["height"] == 0,
        )

        print("== legitimate header ==")
        blk = builder.add_block(signer_labels=["c0-a", "c0-b"])
        show(
            "POST /headers (quorum met)",
            c.post("/headers", json={
                "header": codec.encode_header(blk.header).hex(),
                "certificate": codec.encode_certificate(blk.certificate).hex(),
            }),
            200, lambda b: b["result"]["decision"] == "accepted",
        )

        print("== weight rejection ==")
        low = builder.add_block(signer_labels=["c0-a"])
        show(
            "POST /headers (weight 1 < 2)",
            c.post("/headers", json={
                "header": codec.encode_header(low.header).hex(),
                "certificate": codec.encode_certificate(low.certificate).hex(),
            }),
            409,
            lambda b: b["error"]["code"] == "WEIGHT_BELOW_QUORUM"
            and b["error"]["detail"]["signed_weight"] == 1,
        )

        print("== trust period rejection ==")
        # Build boundary blocks explicitly off the KERNEL's trusted tip,
        # since the rejected weight block above must not move the tip even
        # though the independent builder's append cursor did advance.
        from lightclient.fixtures.builder import build_header, build_certificate

        tip = c.get("/tip").json()["tip"]
        tip_ts = tip["timestamp"]

        def block_off(tip_dct, ts, round_step=5):
            h = build_header(
                chain_id=builder.chain_id,
                height=tip_dct["height"] + 1,
                round=tip_dct["round"] + round_step,
                epoch=tip_dct["epoch"],
                timestamp=ts,
                parent_digest=bytes.fromhex(tip_dct["digest"]),
            )
            sec = builder.committees[tip_dct["epoch"]]
            cert = build_certificate(
                h, [sec.seed_for("c0-a"), sec.seed_for("c0-b")]
            )
            return h, cert

        period = LightClientConfig().trust_period_seconds
        # First, +1s past the period off the CURRENT tip is refused.
        h, cert = block_off(tip, tip_ts + period + 1)
        show(
            "POST /headers (+1s past period -> NEED_CHECKPOINT)",
            c.post("/headers", json={
                "header": codec.encode_header(h).hex(),
                "certificate": codec.encode_certificate(cert).hex(),
            }),
            409, lambda b: b["error"]["code"] == "NEED_CHECKPOINT"
            and b["error"]["detail"]["gap_seconds"] == period + 1,
        )
        # The tip did not move; the exact boundary header now succeeds.
        h, cert = block_off(tip, tip_ts + period)
        show(
            "POST /headers (exact boundary accepted)",
            c.post("/headers", json={
                "header": codec.encode_header(h).hex(),
                "certificate": codec.encode_certificate(cert).hex(),
            }),
            200, lambda b: b["result"]["tip"]["timestamp"] == tip_ts + period,
        )

        print("== malformed input ==")
        show(
            "POST /headers (not hex)",
            c.post("/headers", json={"header": "zz", "certificate": "aa"}),
            400, lambda b: b["error"]["category"] == "input",
        )

        print("== audit trail ==")
        audit = c.get("/audit?limit=20").json()["entries"]
        results = sorted({e["result"] for e in audit})
        print(f"         audit result kinds present: {results}")
        if "rejected" not in results or "accepted" not in results:
            failures.append("audit missing result kinds")
        rejected = [e for e in audit if e["result"] == "rejected"]
        for e in rejected:
            assert e["detail"]["tip_before"] == e["detail"]["tip_after"]

    print(f"\ndb at {tmp/'svc.db'}")
    if failures:
        print(f"HTTP DEMO FAILED: {failures}")
        return 1
    print("HTTP DEMO OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
