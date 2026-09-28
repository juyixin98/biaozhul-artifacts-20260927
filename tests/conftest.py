"""Shared test harness: run identity, environment versions, fixture access.

Every test logs the input file identity (path, size, sha256) and the key
computed values it asserts on, so a log line can always be traced back to
the exact input and reasoning step that produced it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import uuid
from fractions import Fraction

import fastapi
import numpy
import pytest

from fixtures import expected
from mp4timeline.boxes import parse_movie
from mp4timeline.timeline import build_movie_timeline, movie_timeline_to_dict

log = logging.getLogger("mp4timeline.tests")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "data")


def pytest_configure(config):
    config.addinivalue_line("markers", "fixture_case: marks tests bound to a named fixture")


@pytest.fixture(scope="session", autouse=True)
def run_identity():
    rid = uuid.uuid4().hex[:12]
    log.info(
        "RUN %s | python=%s numpy=%s fastapi=%s pytest=%s",
        rid, sys.version.split()[0], numpy.__version__, fastapi.__version__, pytest.__version__,
    )
    return rid


def fixture_path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def load_timeline(name: str) -> dict:
    path = fixture_path(name)
    with open(path, "rb") as fh:
        data = fh.read()
    digest = hashlib.sha256(data).hexdigest()
    log.info("INPUT %s size=%d sha256=%s", name, len(data), digest[:16])
    movie = parse_movie(data)
    result = movie_timeline_to_dict(build_movie_timeline(movie))
    log.info(
        "PARSED %s movie_ts=%d tracks=%d",
        name, result["movie_timescale"], len(result["tracks"]),
    )
    return result


def frac(pair) -> Fraction:
    num, den = pair
    return Fraction(num, den)


def assert_track_matches(actual: dict, ref: dict, run_id: str) -> None:
    """Assert one track's full timeline against the hand-written reference,
    logging each judgment step."""
    assert actual["track_id"] == ref["track_id"]
    assert actual["handler"] == ref["handler"]
    assert actual["media_timescale"] == ref["media_timescale"]

    samples = actual["samples"]
    got = {
        "dts": [s["dts"] for s in samples],
        "cto": [s["cto"] for s in samples],
        "pts": [s["pts"] for s in samples],
        "byte_offsets": [s["byte_offset"] for s in samples],
        "byte_sizes": [s["byte_size"] for s in samples],
        "is_sync": [s["is_sync"] for s in samples],
    }
    for key, want in [("dts", ref["dts"]), ("cto", ref["cto"]), ("pts", ref["pts"]),
                      ("byte_offsets", ref["byte_offsets"]), ("byte_sizes", ref["byte_sizes"]),
                      ("is_sync", ref["is_sync"])]:
        log.info("CHECK run=%s track=%d %s got=%s want=%s", run_id, ref["track_id"], key, got[key], want)
        assert got[key] == want, f"{key}: got {got[key]} want {want}"

    got_gaps = [(frac((g["movie_start"]["num"], g["movie_start"]["den"])),
                 frac((g["movie_end"]["num"], g["movie_end"]["den"]))) for g in actual["gaps"]]
    want_gaps = [(frac(a), frac(b)) for a, b in ref["gaps"]]
    log.info("CHECK run=%s track=%d gaps got=%s want=%s", run_id, ref["track_id"], got_gaps, want_gaps)
    assert got_gaps == want_gaps

    got_pres = [(p["sample_index"],
                 frac((p["movie_start"]["num"], p["movie_start"]["den"])),
                 frac((p["movie_end"]["num"], p["movie_end"]["den"])))
                for p in actual["presentations"]]
    want_pres = [(idx, frac(a), frac(b)) for idx, a, b in ref["presentations"]]
    log.info("CHECK run=%s track=%d presentations got=%s want=%s",
             run_id, ref["track_id"], got_pres, want_pres)
    assert got_pres == want_pres

    got_dur = frac((actual["movie_duration"]["num"], actual["movie_duration"]["den"]))
    want_dur = frac(ref["movie_duration"])
    log.info("CHECK run=%s track=%d movie_duration got=%s want=%s",
             run_id, ref["track_id"], got_dur, want_dur)
    assert got_dur == want_dur


@pytest.fixture()
def check_track(run_identity):
    return lambda actual, ref: assert_track_matches(actual, ref, run_identity)


@pytest.fixture(params=expected.GOOD_FIXTURES, ids=[f["file"] for f in expected.GOOD_FIXTURES])
def good_fixture(request):
    return request.param
