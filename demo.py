#!/usr/bin/env python3
"""Local end-to-end demo: run every fixture through adaptive + fixed buffer.

Usage:
    python demo.py                # all fixtures, compact table
    python demo.py ramp_jitter    # one fixture, detailed per-step trace
    python demo.py --json burst_reorder

No network or accounts needed; all packets are synthesised by ``fixtures``.
"""
from __future__ import annotations

import argparse
import json
import sys

from app.analysis import CATEGORY_DESCRIPTIONS, compare
from app.config import JitterConfig
from app.engine import run_comparison
from fixtures import ALL_FIXTURES, build


def _line(c: str = "-", n: int = 78) -> str:
    return c * n


def run_one(name: str, cfg: JitterConfig) -> dict:
    spec = build(name, cfg)
    results = run_comparison(spec.events, cfg)
    return {"spec": spec, "results": results,
            "verdict": compare(results["adaptive"], results["fixed"])}


def print_header(spec) -> None:
    print(_line("="))
    print(f"FIXTURE  {spec.name}")
    print(f"SCENARIO {spec.description}")
    print(f"PACKETS  sent={spec.expected_packets} "
          f"lost={spec.lost_seqs} duplicated={spec.duplicated_seqs}")
    for note in spec.notes:
        print(f"  note   {note}")
    print(_line("-"))


def print_summary(out: dict) -> None:
    v = out["verdict"]
    print(f"{'mode':<10}{'played':>7}{'gaps':>6}{'dup':>5}{'reord':>7}"
          f"{'late':>6}{'full':>6}{'delay min/mean/max (ms)':>26}")
    for key in ("adaptive", "fixed_baseline"):
        m = v[key]
        d = m["delay_ms"]
        dm = lambda x: f"{x:.1f}" if x is not None else "-"
        print(f"{key:<10}{m['audio_items']:>7}{m['gap_items']:>6}"
              f"{m['drop_categories']['DUPLICATE']:>5}"
              f"{m['drop_categories']['REORDERED']:>7}"
              f"{m['drop_categories']['LATE_AFTER_PLAYOUT']:>6}"
              f"{m['drop_categories']['BUFFER_FULL']:>6}"
              f"{dm(d['min']):>9}/{dm(d['mean'])}/{dm(d['max'])}")
    cmp_ = v["comparison"]
    print(_line("-"))
    print(f"playout monotonic (both): {cmp_['both_monotonic']}   "
          f"buffers bounded (both): {cmp_['both_buffers_bounded']}   "
          f"adaptive within bounds: {cmp_['adaptive_delay_within_bounds']}")
    print(f"gaps adaptive={cmp_['gaps_adaptive']} vs "
          f"fixed={cmp_['gaps_fixed']}   "
          f"late adaptive={cmp_['late_adaptive']} vs "
          f"fixed={cmp_['late_fixed']}")
    for u in v["adaptive"]["uncertainty"]:
        print(f"  UNCERTAINTY: {u}")


def print_detail(out: dict) -> None:
    print_header(out["spec"])
    print_summary(out)
    res = out["results"]["adaptive"]
    print(_line("-"))
    print("first 24 adaptive playout decisions:")
    print(f"{'#':>3} {'kind':<6}{'ssrc':>11}{'seq':>7}{'playout_ms':>13}"
          f"{'delay_ms':>10}")
    for i, p in enumerate(res.playout[:24]):
        print(f"{i:>3} {p.kind:<6}{p.ssrc:>11}{p.ext_seq:>7}"
              f"{p.playout_ms:>13.2f}{p.delay_ms:>10.1f}")
    print(_line("-"))
    print("discard category semantics:")
    for k, desc in CATEGORY_DESCRIPTIONS.items():
        print(f"  {k:<20} {desc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("fixture", nargs="?", default=None,
                    choices=ALL_FIXTURES)
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of text")
    args = ap.parse_args()

    cfg = JitterConfig.from_env()
    names = [args.fixture] if args.fixture else ALL_FIXTURES
    outs = {n: run_one(n, cfg) for n in names}

    if args.json:
        payload = {n: o["verdict"] for n, o in outs.items()}
        print(json.dumps(payload, indent=2, default=str))
        return 0

    for n in names:
        if len(names) == 1:
            print_detail(outs[n])
        else:
            print_header(outs[n]["spec"])
            print_summary(outs[n])
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
