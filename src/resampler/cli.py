"""Command-line client for the resampling service.

Examples
--------
Start the service first:
    uvicorn resampler.api.app:app --host 127.0.0.1 --port 8080

Validate a ratio (shows cutoff, delay, padding):
    python -m resampler.cli validate --base-url http://127.0.0.1:8080 \
        --input-rate 48000 --output-rate 16000

Resample a WAV file (WAV -> WAV, single request body):
    python -m resampler.cli resample-wav --base-url http://127.0.0.1:8080 \
        --input in48k.wav --output-rate 16000 --output out16k.wav

Stream raw f64le mono PCM in arbitrary chunks:
    python -m resampler.cli resample-raw --base-url http://127.0.0.1:8080 \
        --input in.f64 --input-rate 8000 --output-rate 12000 \
        --chunk-samples 37 --output out.f64
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request


def _post(url: str, data: bytes | dict, headers: dict | None = None):
    hdrs = {"Content-Type": "application/octet-stream"}
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _get(url: str, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _show_error(status: int, body: bytes):
    try:
        payload = json.loads(body)
        print(f"[{status}] {json.dumps(payload, indent=2, ensure_ascii=False)}",
              file=sys.stderr)
    except Exception:
        print(f"[{status}] {body!r}", file=sys.stderr)
    sys.exit(1)


def cmd_validate(a):
    status, body, _ = _post(f"{a.base_url}/resample/validate", {
        "input_rate": a.input_rate, "output_rate": a.output_rate})
    if status != 200:
        _show_error(status, body)
    print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))


def cmd_resample_wav(a):
    raw = open(a.input, "rb").read()
    status, body, _ = _post(f"{a.base_url}/jobs", {
        "input_rate": a.input_rate, "output_rate": a.output_rate,
        "input_container": "wav", "output_container": "wav",
        "output_format": a.output_format, "clip_policy": a.clip_policy})
    if status != 201:
        _show_error(status, body)
    job_id = json.loads(body)["job_id"]
    status, body, _ = _post(f"{a.base_url}/jobs/{job_id}/chunks", raw)
    if status != 200:
        _show_error(status, body)
    status, body, _ = _post(f"{a.base_url}/jobs/{job_id}/flush", b"{}",
                            {"Content-Type": "application/json"})
    if status != 200:
        _show_error(status, body)
    status, body, _ = _get(f"{a.base_url}/jobs/{job_id}/result")
    if status != 200:
        _show_error(status, body)
    with open(a.output, "wb") as fh:
        fh.write(body)
    print(f"wrote {a.output} ({len(body)} WAV bytes), job={job_id}")


def cmd_resample_raw(a):
    import numpy as np
    x = np.fromfile(a.input, dtype="<f8")
    status, body, _ = _post(f"{a.base_url}/jobs", {
        "input_rate": a.input_rate, "output_rate": a.output_rate,
        "input_format": "f64le", "output_format": "f64le",
        "clip_policy": a.clip_policy})
    if status != 201:
        _show_error(status, body)
    job_id = json.loads(body)["job_id"]
    c = a.chunk_samples
    for i in range(0, x.size, c):
        payload = np.ascontiguousarray(x[i:i + c], dtype="<f8").tobytes()
        status, body, _ = _post(f"{a.base_url}/jobs/{job_id}/chunks", payload)
        if status != 200:
            _show_error(status, body)
    status, body, _ = _post(f"{a.base_url}/jobs/{job_id}/flush", b"{}",
                            {"Content-Type": "application/json"})
    if status != 200:
        _show_error(status, body)
    status, body, hdr = _get(f"{a.base_url}/jobs/{job_id}/result")
    if status != 200:
        _show_error(status, body)
    y = np.frombuffer(body, dtype="<f8")
    y.tofile(a.output)
    print(f"wrote {a.output}: {x.size} in -> {y.size} out samples, job={job_id}")


def build_parser():
    p = argparse.ArgumentParser(prog="resampler.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--base-url", default="http://127.0.0.1:8080")

    v = sub.add_parser("validate")
    v.add_argument("--input-rate", type=int, required=True)
    v.add_argument("--output-rate", type=int, required=True)
    v.set_defaults(func=cmd_validate)
    add_common(v)

    w = sub.add_parser("resample-wav")
    w.add_argument("--input", required=True)
    w.add_argument("--input-rate", type=int, required=True)
    w.add_argument("--output-rate", type=int, required=True)
    w.add_argument("--output", required=True)
    w.add_argument("--output-format", default="s16le",
                   choices=["u8", "s16le", "s24le", "s32le", "f32le", "f64le"])
    w.add_argument("--clip-policy", default="clip", choices=["clip", "reject"])
    w.set_defaults(func=cmd_resample_wav)
    add_common(w)

    r = sub.add_parser("resample-raw")
    r.add_argument("--input", required=True)
    r.add_argument("--input-rate", type=int, required=True)
    r.add_argument("--output-rate", type=int, required=True)
    r.add_argument("--output", required=True)
    r.add_argument("--chunk-samples", type=int, default=256)
    r.add_argument("--clip-policy", default="clip", choices=["clip", "reject"])
    r.set_defaults(func=cmd_resample_raw)
    add_common(r)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
