#!/usr/bin/env python3
"""Off-line resample a mono PCM WAV file without running the HTTP server.

Uses the same service layer (and therefore the same error/resource contract),
feeding the stream in fixed-size chunks to demonstrate chunk invariance.

    python examples/resample_wav.py input.wav output.wav 48000 [--chunk 1024]

Only 16/24/32-bit mono PCM WAV is accepted (see resamp.media).
"""
from __future__ import annotations

import argparse
import sys

from resamp.config import load_settings
from resamp.media import read_wav, write_wav
from resamp.service import ResamplingService


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input_wav")
    ap.add_argument("output_wav")
    ap.add_argument("fout", type=int)
    ap.add_argument("--chunk", type=int, default=4096)
    ap.add_argument("--sample-width", type=int, default=2, choices=(2, 3, 4))
    args = ap.parse_args()

    settings = load_settings()
    settings.ensure_dirs()
    svc = ResamplingService(settings)
    try:
        pcm = read_wav(args.input_wav)
        print(f"input: {pcm.n_samples} samples @ {pcm.sample_rate}Hz "
              f"({pcm.sample_width * 8}-bit mono)")
        created = svc.create_job(fin=pcm.sample_rate, fout=args.fout)
        jid = created["job"]["job_id"]
        d = created["design"]
        print(f"ratio L={d['l']} M={d['m']} taps={d['numtaps']} "
              f"fc={d['cutoff_hz']:.1f}Hz fstop={d['fstop_hz']:.1f}Hz "
              f"delay={d['group_delay_seconds']*1000:.3f}ms")
        for i in range(0, pcm.samples.size, args.chunk):
            svc.push(jid, pcm.samples[i:i + args.chunk])
        svc.flush(jid)
        y = svc.store.read_output(jid)
        blob = write_wav(y, args.fout, sample_width=args.sample_width)
        with open(args.output_wav, "wb") as fh:
            fh.write(blob)
        print(f"output: {y.size} samples @ {args.fout}Hz -> {args.output_wav}")
    finally:
        svc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
