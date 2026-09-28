#!/usr/bin/env python3
"""Local end-to-end demonstration.

Every scenario starts from the same trusted checkpoint on a fresh in-memory
client (the only way to obtain a root is the out-of-band checkpoint), which
mirrors how a real light client evaluates an independently captured update.

Scenarios:
  1. install a trusted checkpoint (out-of-band),
  2. follow continuous legal headers,
  3. reject an under-weight certificate (40 < 41) — tip unchanged,
  4. rotate the committee under the OLD committee's quorum, then accept the
     NEW committee at its own threshold,
  5. reject the OLD committee signing after rotation (SIGNER_UNKNOWN),
  6. reject an untrusted branch (unknown parent) — root never moves,
  7. simulate a long offline period -> TRUST_EXPIRED / needs new checkpoint,
  8. print the replayable run log (run id + intermediate state + reasons).

Run:
    .venv/bin/python scripts/demo.py
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import oracle  # noqa: E402  (independent reference; imports no lc.* code)
from lc.chain import ChainKernel  # noqa: E402
from lc.clock import FixedClock, RunRecorder  # noqa: E402
from lc.config import KernelConfig  # noqa: E402
from lc.errors import LightClientError  # noqa: E402
from lc.store import Store  # noqa: E402
from lc.types import Certificate, Checkpoint, Committee, Header  # noqa: E402

GREEN = "\033[92m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def hr(title: str) -> None:
    print(f"\n{BOLD}{CYAN}=== {title} ==={RESET}")


def fresh_client(golden, log_dir, *, now_offset_slots=400):
    g = golden["constants"]
    clock = FixedClock(g["T0"] + now_offset_slots * g["SLOT_MS"])
    recorder = RunRecorder(log_dir=log_dir)
    store = Store(":memory:")
    kernel = ChainKernel(
        store, clock, KernelConfig(trust_period_ms=g["trust_period_ms"]), recorder
    )
    cp = Checkpoint.from_dict(golden["checkpoint"], committee_max_size=256)
    kernel.install_checkpoint(cp)
    return kernel, clock, recorder


def vec(golden, vid):
    return next(v for v in golden["vectors"] if v["id"] == vid)


def tip_key(kernel):
    t = kernel.store.get_tip()
    return (t.tip_header_root, t.tip_round, t.active_committee_commitment)


def show_tip(kernel):
    h = kernel.head()
    print(
        f"  tip round={h['tip_round']} root=0x…{h['tip_header_root'][-12:]} "
        f"seq={h['sequence']} fresh={h.get('fresh')}"
    )


def apply_vec(kernel, v):
    header = Header.from_dict(v["header"])
    cert = Certificate.from_dict(v["certificate"])
    nc = (
        None
        if not v.get("next_committee")
        else Committee.from_dict(v["next_committee"], max_size=256)
    )
    return kernel.apply_header(header, cert, nc)


def expect_accept(label, kernel, v):
    try:
        rep = apply_vec(kernel, v)
        print(f"  {GREEN}ACCEPT{RESET} {label} (round {rep.round})")
        return rep
    except LightClientError as exc:
        print(f"  {RED}UNEXPECTED REJECT{RESET} {label}: {exc.code.value}")
        raise


def expect_reject(label, kernel, v, expected_code):
    before = tip_key(kernel)
    try:
        apply_vec(kernel, v)
        print(f"  {RED}UNEXPECTED ACCEPT{RESET} {label}")
        raise SystemExit(1)
    except LightClientError as exc:
        ok = exc.code.value == expected_code
        mark = GREEN + "OK  " if ok else RED + "BAD "
        print(
            f"  {mark}{RESET}{label}: {exc.code.value} "
            f"({exc.category.value})"
        )
        print(f"        reason: {exc.message}")
        unchanged = tip_key(kernel) == before
        print(f"        trusted tip unchanged: {unchanged}")
        if not (ok and unchanged):
            raise SystemExit(1)


def main() -> int:
    log_dir = os.path.join(ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    golden = oracle.build_golden()

    hr("run setup")
    print(f"  protocol   : {golden['protocol']} (simplified, not a public chain)")
    print(f"  threshold  : {golden['constants']['base_required_weight']} weight "
          f"of {golden['constants']['base_total_weight']} (floor(2W/3)+1)")

    hr("1. follow continuous legal headers (5/6 = 50w >= 41w)")
    k, _, rec = fresh_client(golden, log_dir)
    print(f"  run_id = {rec.run_id}")
    for item in vec(golden, "legal_continuous_3")["items"]:
        rep = expect_accept(f"round {item['header']['round']}", k, item)
    show_tip(k)

    hr("2. under-weight certificate at the boundary (4/6 = 40w < 41w)")
    k, _, _ = fresh_client(golden, log_dir)
    expect_reject("4-of-6 cert", k, vec(golden, "weight_below_threshold"),
                  "INSUFFICIENT_WEIGHT")

    hr("3. committee rotation authorized by the PRIOR committee")
    k, _, _ = fresh_client(golden, log_dir)
    rep = expect_accept("rotation header (5/6 old quorum)", k,
                        vec(golden, "committee_rotation_authorized"))
    print(f"        new active committee 0x…{rep.active_committee_after[-12:]}")
    # the OLD committee must no longer authorize anything
    expect_reject("old committee signs next header", k,
                  vec(golden, "old_committee_signs_after_rotation"),
                  "SIGNER_UNKNOWN")
    # under-weight by the NEW committee (3/5 = 30 < 34)
    expect_reject("new committee 3/5 = 30w", k,
                  vec(golden, "new_committee_underweight"),
                  "INSUFFICIENT_WEIGHT")
    # over the NEW committee's threshold (4/5 = 40 >= 34)
    expect_accept("new committee 4/5 = 40w", k,
                  vec(golden, "new_committee_authorized"))
    show_tip(k)

    hr("4. untrusted branch / equivocation can never move the root")
    k, _, _ = fresh_client(golden, log_dir)
    expect_reject("unknown parent", k,
                  vec(golden, "untrusted_branch_unknown_parent"),
                  "UNTRUSTED_BRANCH")
    expect_reject("stale round 50", k, vec(golden, "stale_round"),
                  "STALE_ROUND")
    expect_reject("bad signature", k, vec(golden, "bad_signature"),
                  "CRYPTO_BAD_SIGNATURE")
    expect_reject("cert bound elsewhere", k,
                  vec(golden, "cert_bound_to_other_header"),
                  "CERT_BIND_MISMATCH")
    expect_reject("rotation commitment mismatch", k,
                  vec(golden, "rotation_commitment_mismatch"),
                  "ROTATION_COMMITMENT_MISMATCH")
    # same-round fork after round 101 is accepted
    expect_accept("round 101 (prerequisite)", k,
                  vec(golden, "legal_continuous_3")["items"][0])
    expect_reject("fork at round 101", k,
                  vec(golden, "conflict_same_round_equivocation"),
                  "CONFLICT_EQUIVOCATION")

    hr("5. long offline -> beyond trust period -> new checkpoint required")
    k, clock, rec = fresh_client(golden, log_dir, now_offset_slots=0)
    print(f"  run_id = {rec.run_id}")
    t0 = golden["checkpoint"]["header"]["timestamp"]
    period = golden["constants"]["trust_period_ms"]
    clock.set(t0 + period + 1)
    print(f"  trust: {json.dumps(k.trust_status(), sort_keys=True)}")
    chain0 = vec(golden, "legal_continuous_3")["items"][0]
    expect_reject("peer update after long offline", k, chain0, "TRUST_EXPIRED")
    print("        -> the ONLY remedy is a new out-of-band checkpoint "
          "(fresh DB)")

    hr("6. replayable run log (decision trace from the offline client)")
    last = rec.snapshot()[-1]
    print(json.dumps(last, indent=2, sort_keys=True)[:1400])

    print(f"\n{BOLD}{GREEN}demo complete{RESET}; JSONL logs written to {log_dir}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
