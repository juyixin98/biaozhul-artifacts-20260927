#!/usr/bin/env python3
"""Generate the sample fixture set under data/fixtures/.

Produces three scenarios:
  clean/       drift + offset only
  corrupted/   additionally one dropped-frame gap and one spurious pulse
  degenerate/  only two sync pulses -> must yield INSUFFICIENT evidence
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from driftcorr.fixtures.synth import (  # noqa: E402
    DropSpec, FixtureSpec, SpuriousPulse, generate_fixture,
    reference_metadata, target_metadata,
)
from driftcorr.media.wav_io import write_wav  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "data" / "fixtures"

SCENARIOS = {
    "clean": FixtureSpec(),
    "corrupted": FixtureSpec(
        drops=(DropSpec(at_target_time_s=7.32, duration_s=0.025),),
        spurious=(SpuriousPulse(at_target_time_s=3.55, amplitude=1.4),),
    ),
    "degenerate": FixtureSpec(pulse_times_s=(0.5, 7.5)),
}


def main() -> None:
    for name, spec in SCENARIOS.items():
        fx = generate_fixture(spec)
        d = OUT / name
        d.mkdir(parents=True, exist_ok=True)
        write_wav(d / "reference.wav", fx.reference, fx.fs)
        write_wav(d / "target.wav", fx.target, fx.fs)
        (d / "reference.meta.json").write_text(
            json.dumps(reference_metadata(spec), indent=2), encoding="utf-8")
        (d / "target.meta.json").write_text(
            json.dumps(target_metadata(spec), indent=2), encoding="utf-8")
        (d / "ground_truth.json").write_text(
            json.dumps(fx.ground_truth, indent=2), encoding="utf-8")
        print(f"{name}: wrote {d} "
                  f"(ref {len(fx.reference)} samples, tgt {len(fx.target)} samples)")


if __name__ == "__main__":
    main()
