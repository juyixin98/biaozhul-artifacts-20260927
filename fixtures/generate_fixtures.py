#!/usr/bin/env python3
"""Generate the minimal deterministic segment-descriptor fixtures.

Run:  .venv/bin/python fixtures/generate_fixtures.py

The numbers in these tables are deliberately simple so that every
expected timestamp in the test suite is hand-computable.  Time bases:
video 1/90000 with 3000-tick frames (exactly 30 fps), audio 1/48000 with
1024-sample AAC frames.
"""
from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"

VIDEO_TB = [1, 90000]
FRAME = 3000
REORDER = 2  # B-frame reorder depth -> source DTS start at -2*FRAME


def video_sample(index: int, dts: int, pts: int, deps: list[int],
                 keyframe: bool = False, idr: bool = False) -> dict:
    return {"dts": dts, "pts": pts, "duration": FRAME, "keyframe": keyframe,
            "idr": idr, "depends_on": deps}


def video_ok() -> list[dict]:
    """Closed-GOP H.264 stream, 10 displayed frames, B-frame reorder delay.

    display: I0 B1 B2 P3 B4 B5 I6 B7 B8 P9
    decode : I0 P3 B1 B2 I6 B4 B5 P9 B7 B8
    """
    f = FRAME
    return [
        video_sample(0, dts=(0 - REORDER) * f, pts=0 * f,  deps=[], keyframe=True, idr=True),
        video_sample(1, dts=(1 - REORDER) * f, pts=3 * f,  deps=[0]),
        video_sample(2, dts=(2 - REORDER) * f, pts=1 * f,  deps=[0, 1]),
        video_sample(3, dts=(3 - REORDER) * f, pts=2 * f,  deps=[0, 1]),
        video_sample(4, dts=(4 - REORDER) * f, pts=6 * f,  deps=[], keyframe=True, idr=True),
        video_sample(5, dts=(5 - REORDER) * f, pts=4 * f,  deps=[1, 4]),
        video_sample(6, dts=(6 - REORDER) * f, pts=5 * f,  deps=[1, 4]),
        video_sample(7, dts=(7 - REORDER) * f, pts=9 * f,  deps=[4]),
        video_sample(8, dts=(8 - REORDER) * f, pts=7 * f,  deps=[4, 7]),
        video_sample(9, dts=(9 - REORDER) * f, pts=8 * f,  deps=[4, 7]),
    ]


def video_open_gop() -> list[dict]:
    """Open-GOP stream: sample K6 is a non-IDR keyframe and the leading
    pictures B7/B8 (presented after K6) reference P3 (before K6)."""
    f = FRAME
    return [
        video_sample(0, dts=(0 - REORDER) * f, pts=0 * f, deps=[], keyframe=True, idr=True),
        video_sample(1, dts=(1 - REORDER) * f, pts=3 * f, deps=[0]),
        video_sample(2, dts=(2 - REORDER) * f, pts=1 * f, deps=[0, 1]),
        video_sample(3, dts=(3 - REORDER) * f, pts=2 * f, deps=[0, 1]),
        # K6: open-GOP recovery point (keyframe, NOT idr)
        video_sample(4, dts=(4 - REORDER) * f, pts=6 * f, deps=[], keyframe=True, idr=False),
        video_sample(5, dts=(5 - REORDER) * f, pts=4 * f, deps=[1, 4]),
        video_sample(6, dts=(6 - REORDER) * f, pts=5 * f, deps=[1, 4]),
        video_sample(7, dts=(7 - REORDER) * f, pts=9 * f, deps=[4]),
        # open GOP: reference P3 (index 1) which is BEFORE keyframe K6
        video_sample(8, dts=(8 - REORDER) * f, pts=7 * f, deps=[1, 7]),
        video_sample(9, dts=(9 - REORDER) * f, pts=8 * f, deps=[1, 7]),
    ]


def audio_ok(n_frames: int, encoder_delay: int = 1024) -> list[dict]:
    """AAC-LC 48 kHz stereo frames of 1024 samples; first frame(s) carry
    the encoder delay and have negative PTS."""
    delay_frames = encoder_delay // 1024
    return [
        {"dts": i * 1024, "pts": (i - delay_frames) * 1024,
         "duration": 1024, "keyframe": False}
        for i in range(n_frames)
    ]


def video_stream(samples: list[dict], *, codec: str = "h264",
                 profile: str = "high", level: int = 40,
                 time_base: list[int] | None = None) -> dict:
    return {
        "type": "video", "codec": codec, "profile": profile, "level": level,
        "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
        "time_base": time_base or VIDEO_TB, "samples": samples,
    }


def audio_stream(n_frames: int, *, encoder_delay: int = 1024) -> dict:
    return {
        "type": "audio", "codec": "aac", "profile": "lc",
        "sample_rate": 48000, "channels": 2,
        "time_base": [1, 48000],
        "encoder_delay": encoder_delay,
        "samples": audio_ok(n_frames, encoder_delay),
    }


def descriptor(name: str, streams: list[dict]) -> dict:
    return {"name": name, "container": "mp4", "streams": streams}


def video_simple_25fps() -> list[dict]:
    """No B-frames, time base 1/25 — exercises time-base mismatch."""
    out = []
    for i in range(10):
        out.append({
            "dts": i, "pts": i, "duration": 1,
            "keyframe": i % 5 == 0, "idr": i % 5 == 0,
            "depends_on": ([] if i % 5 == 0 else [i - 1]),
        })
    return out


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fixtures = {
        # two compatible closed-GOP segments; B has a short audio tail
        # so the planner must add exactly one padding frame
        "seg_ok_a.json": descriptor("seg_ok_a", [
            video_stream(video_ok()), audio_stream(17)]),
        "seg_ok_b.json": descriptor("seg_ok_b", [
            video_stream(video_ok()), audio_stream(16)]),
        # named fixture for the non-keyframe-trim case
        "seg_midgop.json": descriptor("seg_midgop", [
            video_stream(video_ok()), audio_stream(17)]),
        # open GOP recovery point
        "seg_open_gop.json": descriptor("seg_open_gop", [
            video_stream(video_open_gop()), audio_stream(17)]),
        # 2048-sample audio encoder delay (two priming frames)
        "seg_audio_delay.json": descriptor("seg_audio_delay", [
            video_stream(video_ok()), audio_stream(18, encoder_delay=2048)]),
        # incompatible: different video time base
        "seg_tb_mismatch.json": descriptor("seg_tb_mismatch", [
            video_stream(video_simple_25fps(), time_base=[1, 25]),
            audio_stream(17)]),
        # incompatible: different codec
        "seg_codec_mismatch.json": descriptor("seg_codec_mismatch", [
            video_stream(video_ok(), codec="hevc", profile="main", level=120),
            audio_stream(17)]),
        # broken: references a sample that does not exist
        "seg_dangling_ref.json": descriptor(
            "seg_dangling_ref",
            [video_stream([
                {**s, "depends_on": [1, 99]} if i == 5 else s
                for i, s in enumerate(video_ok())]), audio_stream(17)]),
    }
    for filename, doc in fixtures.items():
        path = DATA_DIR / filename
        path.write_text(json.dumps(doc, indent=2) + "\n")
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
