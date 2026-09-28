"""Timeline-kernel tests: every good fixture's full timeline is asserted
against the hand-written reference in fixtures/expected.py."""

import logging

from fixtures import expected

from .conftest import load_timeline

log = logging.getLogger("mp4timeline.tests")


def test_full_timeline_matches_hand_reference(good_fixture, check_track):
    result = load_timeline(good_fixture["file"])
    assert result["movie_timescale"] == good_fixture["movie_timescale"]
    assert len(result["tracks"]) == len(good_fixture["tracks"])
    for actual_track, ref_track in zip(result["tracks"], good_fixture["tracks"]):
        check_track(actual_track, ref_track)


def test_bframe_reorder_presentation_order(check_track):
    result = load_timeline(expected.BFRAMES["file"])
    track = result["tracks"][0]
    # decode order is sample index order; presentation order must follow PTS
    pts = [s["pts"] for s in track["samples"]]
    assert pts == [6000, 3000, 9000, 18000]
    order = [p["sample_index"] for p in track["presentations"]]
    assert order == [1, 0, 2, 3], f"presentation order {order} != [1, 0, 2, 3]"


def test_signed_ctts_keeps_negative_pts(check_track):
    result = load_timeline(expected.CTTS_SIGNED["file"])
    track = result["tracks"][0]
    ctos = [s["cto"] for s in track["samples"]]
    pts = [s["pts"] for s in track["samples"]]
    assert min(ctos) < 0, "fixture must contain a negative composition offset"
    assert min(pts) == -1000, f"negative PTS not preserved: {pts}"


def test_empty_edit_shifts_media_and_creates_gap():
    result = load_timeline(expected.EMPTY_EDIT["file"])
    track = result["tracks"][0]
    gap = track["gaps"][0]
    assert (gap["movie_start"], gap["movie_end"]) == ({"num": 0, "den": 1}, {"num": 2000, "den": 1})
    first = track["presentations"][0]
    assert first["movie_start"] == {"num": 2000, "den": 1}


def test_rational_timescale_conversion_is_exact():
    # audio 48000 -> movie 1000: sample duration 1024 units = 64/3 movie units,
    # a non-integer rational that float arithmetic would smear
    result = load_timeline(expected.MULTITRACK["file"])
    audio = result["tracks"][1]
    assert audio["media_timescale"] == 48000
    first = audio["presentations"][0]
    assert first["movie_end"] == {"num": 64, "den": 3}
    last = audio["presentations"][-1]
    assert last["movie_end"] == {"num": 128, "den": 1}


def test_trim_window_excludes_boundary_samples():
    result = load_timeline(expected.TRIM["file"])
    track = result["tracks"][0]
    presented = [p["sample_index"] for p in track["presentations"]]
    assert presented == [1], f"only sample 1 (pts 1000) lies in [500, 2000): {presented}"
    pres = track["presentations"][0]
    assert pres["movie_start"] == {"num": 500, "den": 1}
    assert pres["movie_end"] == {"num": 1500, "den": 1}


def test_sample_byte_ranges_point_at_sample_payload():
    # byte ranges must locate the deterministic payload bytes written by the
    # fixture builder (sample i of track t is filled with byte t*16+i)
    import os

    from .conftest import fixture_path

    for spec in expected.GOOD_FIXTURES:
        path = fixture_path(spec["file"])
        with open(path, "rb") as fh:
            data = fh.read()
        result = load_timeline(spec["file"])
        for t_idx, track in enumerate(result["tracks"]):
            for s in track["samples"]:
                chunk = data[s["byte_offset"]: s["byte_offset"] + s["byte_size"]]
                want = bytes([(t_idx * 16 + s["index"]) & 0xFF]) * s["byte_size"]
                assert chunk == want, (
                    f"{spec['file']} track {track['track_id']} sample {s['index']}: "
                    "byte range does not cover the sample payload"
                )
