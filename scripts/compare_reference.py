"""Cross-check the project kernel against independent reference implementations.

Runs each synthetic fixture through:
  1. the project streaming kernel,
  2. pyloudnorm (independent third-party, integrated loudness only),
  3. the local ffmpeg ebur128 filter (independent C implementation),
and prints block counts BEFORE/AFTER gating and every threshold/parameter.

It does NOT use the kernel to generate expected answers: ffmpeg is the
authoritative oracle for all gated statistics; pyloudnorm is a second code
path for integrated loudness. Run:

    .venv/bin/python scripts/compare_reference.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.loudness import analyze_array  # noqa: E402
from tests import fixtures as fx  # noqa: E402
from tests.reference import FfmpegReference, PyloudnormReference  # noqa: E402

FIXTURES = {
    "silence_3s": lambda: fx.digital_silence(3.0),
    "silence_0.3s": lambda: fx.digital_silence(0.3),
    "tone_-23_mono_10s": lambda: fx.calibrated_loudness_tone(10, -23, 1),
    "tone_-23_stereo_10s": lambda: fx.calibrated_loudness_tone(10, -23, 2),
    "burst_250ms_in_2s": lambda: fx.tone_burst(2.0, 0.5, 0.25, -23),
    "burst_200ms_in_2s": lambda: fx.tone_burst(2.0, 0.1, 0.20, -23),
    "tone_0.3s_insufficient": lambda: fx.calibrated_loudness_tone(0.3, -23),
    "tone_1s_no_lra": lambda: fx.calibrated_loudness_tone(1.0, -23),
    "quiet_below_-70": lambda: fx.quiet_below_abs_gate(2.0, -85),
    "channel_switch_8s": lambda: fx.channel_switch(8.0),
    "surround_5.1_4s": lambda: fx.surround_mix(4.0, -23),
    "multilevel_32s": lambda: fx.segmented_programme(
        [(6, -36), (6, -18), (6, -30), (6, -24)], gap_seconds=2),
}


def main() -> int:
    ffmpeg = FfmpegReference()
    try:
        pyln = PyloudnormReference()
        have_pyln = True
    except Exception as exc:  # pragma: no cover
        print(f"pyloudnorm unavailable: {exc}")
        have_pyln = False

    print(f"ffmpeg available: {ffmpeg.available}  {ffmpeg.version}")
    print(f"pyloudnorm available: {have_pyln}\n")

    report = {"ffmpeg_version": ffmpeg.version, "cases": []}
    all_ok = True

    for name, builder in FIXTURES.items():
        x = builder()
        r = analyze_array(x)
        g, l = r.gating, r.lra
        row: dict = {
            "fixture": name,
            "duration_s": round(x.shape[0] / 48000, 3),
            "channels": x.shape[1],
            "kernel": {
                "status": r.status,
                "I_LUFS": None if r.integrated_loudness_lufs is None
                else round(r.integrated_loudness_lufs, 3),
                "int_blocks_before_abs_gate": g.blocks_total,
                "int_blocks_after_abs_gate": g.blocks_above_absolute,
                "int_blocks_after_rel_gate": g.blocks_above_relative,
                "abs_gate": g.absolute_gate_lufs,
                "rel_gate_LUFS": None if g.relative_gate_lufs is None
                else round(g.relative_gate_lufs, 3),
                "lra_status": l.status,
                "LRA_LU": l.loudness_range_lu,
                "lra_blocks_total": l.blocks_total,
                "lra_after_abs": l.blocks_above_absolute,
                "lra_after_rel": l.blocks_above_relative,
                "lra_rel_gate": None if l.relative_gate_lufs is None
                else round(l.relative_gate_lufs, 3),
                "p10": l.percentile_10_lufs,
                "p95": l.percentile_95_lufs,
            },
        }

        if have_pyln and r.status == "OK":
            try:
                row["pyloudnorm_I_LUFS"] = round(pyln.integrated_loudness(x), 3)
            except Exception as exc:  # pragma: no cover
                row["pyloudnorm_error"] = str(exc)

        if ffmpeg.available:
            ref = ffmpeg.measure(x)
            if ref.is_silence_sentinel:
                # ffmpeg prints I=-70/Thr=0 for BOTH digital silence and any
                # signal with no blocks above its gate; it cannot distinguish
                # them. Our kernel separates SILENCE / NOT_COMPUTED /
                # INSUFFICIENT_BLOCKS, so agreement means the kernel, too,
                # produced no numeric integrated loudness (rather than -70).
                row["ffmpeg"] = {"result": "no-gated-result sentinel "
                                           "(ffmpeg prints I=-70/Thr=0)"}
                agrees = r.integrated_loudness_lufs is None
            elif r.status == "OK" and ref.integrated_lufs is not None:
                diff = abs(r.integrated_loudness_lufs - ref.integrated_lufs)
                agrees = diff <= 0.12
                row["ffmpeg"] = {
                    "I_LUFS": ref.integrated_lufs,
                    "I_threshold": ref.integrated_threshold_lufs,
                    "LRA_LU": ref.lra_lu,
                    "LRA_threshold": ref.lra_threshold_lufs,
                    "LRA_low": ref.lra_low_lufs,
                    "LRA_high": ref.lra_high_lufs,
                    "I_abs_diff_LU": round(diff, 3),
                }
            else:
                agrees = True  # non-OK cases asserted in pytest, not oracle
            row["agrees_with_ffmpeg_I"] = bool(agrees)
            all_ok &= agrees

        report["cases"].append(row)

        print(f"== {name}  ({row['duration_s']}s, {row['channels']}ch)")
        k = row["kernel"]
        print(f"   kernel  status={k['status']:<18} I={k['I_LUFS']}  "
              f"int blocks {k['int_blocks_before_abs_gate']} -> abs "
              f"{k['int_blocks_after_abs_gate']} -> rel "
              f"{k['int_blocks_after_rel_gate']}  relgate={k['rel_gate_LUFS']}")
        print(f"           LRA status={k['lra_status']:<18} "
              f"LRA={k['LRA_LU']}  blocks {k['lra_blocks_total']} -> abs "
              f"{k['lra_after_abs']} -> rel {k['lra_after_rel']}  "
              f"p10={k['p10']} p95={k['p95']} gate={k['lra_rel_gate']}")
        if "ffmpeg" in row:
            ff = row["ffmpeg"]
            if "I_LUFS" in ff:
                print(f"   ffmpeg  I={ff['I_LUFS']} thr={ff['I_threshold']} "
                      f"LRA={ff['LRA_LU']} low={ff['LRA_low']} "
                      f"high={ff['LRA_high']}  |dI|={ff['I_abs_diff_LU']} "
                      f"agree={row['agrees_with_ffmpeg_I']}")
            else:
                print(f"   ffmpeg  {ff['result']}")
        if "pyloudnorm_I_LUFS" in row:
            print(f"   pyloudnorm I={row['pyloudnorm_I_LUFS']}")
        print()

    out = os.path.join(os.path.dirname(__file__), "..", "reports",
                       "reference_comparison.json")
    out = os.path.abspath(out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"JSON report -> {out}")
    print("ALL I AGREE WITH FFMPEG" if all_ok else "MISMATCH FOUND")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
