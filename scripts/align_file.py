#!/usr/bin/env python3
"""Run an alignment directly on local files (no HTTP), print the report.

Examples:
  python scripts/align_file.py --stereo samples/drift_offset.wav
  python scripts/align_file.py --ref a.wav --slave b.wav --mode pulses
"""
import argparse
import json
import sys
from pathlib import Path

from clockalign.config import load_config
from clockalign.logging_setup import configure_logging
from clockalign.media import load_pair
from clockalign.pipeline import run_alignment
from clockalign.jobs import _overlay
from clockalign.media import write_pcm_wav

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ref")
    p.add_argument("--slave")
    p.add_argument("--stereo")
    p.add_argument("--mode", choices=["pulses", "correlate", "auto"])
    p.add_argument("--anchor", action="store_true",
                   help="declare an external absolute-time anchor")
    p.add_argument("--out-dir", default=None,
                   help="directory for corrected_slave.wav/overlay/timeline")
    args = p.parse_args()

    configure_logging()
    cfg = load_config()
    ref, slv = load_pair(args.ref, args.slave, stereo_path=args.stereo,
                         stereo_role=cfg.media.stereo_channel_role,
                         max_sample_rate=cfg.media.max_sample_rate,
                         sample_rate_mismatch_ppm_max=(
                             cfg.media.sample_rate_mismatch_ppm_max))
    result = run_alignment(ref, slv, cfg, mode=args.mode,
                           external_time_anchor=args.anchor)
    if args.out_dir and result.correction is not None:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        write_pcm_wav(out / "corrected_slave.wav",
                      result.correction.corrected_audio,
                      result.correction.sample_rate)
        write_pcm_wav(out / "overlay.wav",
                      _overlay(ref.samples, result.correction.corrected_audio),
                      result.correction.sample_rate)
        with open(out / "timeline_map.json", "w") as fh:
            json.dump(result.timeline_map, fh, indent=2, sort_keys=True)
    json.dump(result.to_dict(), sys.stdout, indent=2, sort_keys=True)
    print()
    sys.exit(0 if result.status == "corrected" else 2)
