"""Generate the synthetic MP4 fixtures into fixtures/data/.

Run:  python fixtures/make_fixtures.py
"""

from __future__ import annotations

import os
import struct

from .builder import TrackSpec, box, ftyp, fullbox, make_file, stts

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _write(name: str, data: bytes) -> str:
    path = os.path.join(DATA_DIR, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def build_all() -> list[str]:
    os.makedirs(DATA_DIR, exist_ok=True)
    written = []

    # --- bframes.mp4: ctts v0 B-frame reorder -------------------------------
    written.append(_write("bframes.mp4", make_file(
        [TrackSpec(
            track_id=1, handler=b"vide", media_timescale=90000, media_duration=21000,
            sample_sizes=[100, 20, 25, 90],
            stts_entries=[(4, 3000)],
            ctts_entries=[(1, 6000), (1, 0), (1, 3000), (1, 9000)], ctts_version=0,
            stss_samples=[1],
        )],
        movie_timescale=1000, movie_duration=233,
    )))

    # --- empty_edit.mp4: leading empty edit ---------------------------------
    written.append(_write("empty_edit.mp4", make_file(
        [TrackSpec(
            track_id=1, handler=b"vide", media_timescale=1000, media_duration=3000,
            sample_sizes=[10, 11, 12],
            stts_entries=[(3, 1000)],
            elst_entries=[(2000, -1, 1, 0), (3000, 0, 1, 0)], elst_version=0,
        )],
        movie_timescale=1000, movie_duration=5000,
    )))

    # --- multitrack.mp4: video 90000 + audio 48000, ctts v1 reorder ---------
    written.append(_write("multitrack.mp4", make_file(
        [
            TrackSpec(
                track_id=1, handler=b"vide", media_timescale=90000, media_duration=12000,
                sample_sizes=[50, 30, 60],
                stts_entries=[(3, 3000)],
                ctts_entries=[(1, 3000), (1, -3000), (1, 3000)], ctts_version=1,
            ),
            TrackSpec(
                track_id=2, handler=b"soun", media_timescale=48000, media_duration=6144,
                sample_sizes=[8, 8, 8, 8, 8, 8],
                stts_entries=[(6, 1024)],
                stsd_entry=b"mp4a",
            ),
        ],
        movie_timescale=1000, movie_duration=128,
    )))

    # --- ctts_signed.mp4: negative ctts v1 offsets, elst v1, co64 -----------
    written.append(_write("ctts_signed.mp4", make_file(
        [TrackSpec(
            track_id=1, handler=b"vide", media_timescale=1000, media_duration=3000,
            sample_sizes=[40, 41, 42],
            stts_entries=[(3, 1000)],
            ctts_entries=[(1, 2000), (1, -2000), (1, 4000)], ctts_version=1,
            elst_entries=[(8000, -1000, 1, 0)], elst_version=1,
            use_co64=True,
        )],
        movie_timescale=1000, movie_duration=8000,
    )))

    # --- trim.mp4: edit window cuts mid-stream, two chunks ------------------
    written.append(_write("trim.mp4", make_file(
        [TrackSpec(
            track_id=1, handler=b"vide", media_timescale=1000, media_duration=4000,
            sample_sizes=[10, 20, 30, 40],
            stts_entries=[(4, 1000)],
            stsc_entries=[(1, 3, 1), (2, 1, 1)],
            elst_entries=[(1500, 500, 1, 0)], elst_version=0,
        )],
        movie_timescale=1000, movie_duration=1500,
    )))

    # --- bad_boxlen.mp4: moov declares a size beyond EOF --------------------
    written.append(_write(
        "bad_boxlen.mp4",
        ftyp() + struct.pack(">I4s", 100000, b"moov") + b"\x00" * 16,
    ))

    # --- fragmented.mp4: mvex inside moov + trailing moof -------------------
    frag_moov = box(b"moov", box(b"mvex", box(b"trex", b"\x00" * 24)))
    moof = box(b"moof", box(b"mfhd", b"\x00" * 8))
    written.append(_write("fragmented.mp4", ftyp() + box(b"mdat", b"") + frag_moov + moof))

    # --- encrypted.mp4: encrypted sample entry ------------------------------
    written.append(_write("encrypted.mp4", make_file(
        [TrackSpec(
            track_id=1, handler=b"vide", media_timescale=1000, media_duration=1000,
            sample_sizes=[8],
            stts_entries=[(1, 1000)],
            stsd_entry=b"encv",
        )],
        movie_timescale=1000, movie_duration=1000,
    )))

    # --- bad_stts.mp4: stts entry_count exceeds the box payload -------------
    bad_stts_payload = struct.pack(">I", 5) + struct.pack(">II", 1, 1000)  # says 5, holds 1
    bad_stts_box = fullbox(b"stts", 0, 0, bad_stts_payload)
    # splice the corrupt stts into an otherwise valid single-track file
    good = make_file(
        [TrackSpec(
            track_id=1, handler=b"vide", media_timescale=1000, media_duration=1000,
            sample_sizes=[8], stts_entries=[(1, 1000)],
        )],
        movie_timescale=1000, movie_duration=1000,
    )
    good_stts = stts([(1, 1000)])
    assert good_stts in good
    written.append(_write("bad_stts.mp4", good.replace(good_stts, bad_stts_box)))

    # --- bad_rate.mp4: edit rate 2.0 (unsupported) ---------------------------
    written.append(_write("bad_rate.mp4", make_file(
        [TrackSpec(
            track_id=1, handler=b"vide", media_timescale=1000, media_duration=1000,
            sample_sizes=[8],
            stts_entries=[(1, 1000)],
            elst_entries=[(500, 0, 2, 0)], elst_version=0,
        )],
        movie_timescale=1000, movie_duration=500,
    )))

    return written


if __name__ == "__main__":
    for path in build_all():
        print(f"wrote {path} ({os.path.getsize(path)} bytes)")
