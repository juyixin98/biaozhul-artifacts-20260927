"""Tests for diagnostic detection: concrete codes and conflict magnitudes."""
from __future__ import annotations

from pathlib import Path

from app.core.diagnosis import diagnose
from app.core.models import Severity
from app.media import parse_bytes

FIX = Path(__file__).parent / "fixtures"

SETTINGS_KW = dict(
    min_duration_ms=1000,
    max_duration_ms=7000,
    segment_boundaries_ms=(30_000, 60_000),
    horizon_ms=90_000,
    min_gap_ms=1,
)


def diagnose_fixture(name):
    r = parse_bytes((FIX / name).read_bytes())
    assert r.ok, [d.message for d in r.diagnostics if d.severity == Severity.ERROR]
    return r, diagnose(r.cues, **SETTINGS_KW)


def test_chain_overlap_reports_every_adjacent_pair():
    _, diags = diagnose_fixture("chain_overlap.srt")
    overlaps = [d for d in diags if d.code == "overlap"]
    # cues 1..4 form a start-ordered chain -> three adjacent conflicts
    assert len(overlaps) == 3
    pairs = {(d.cue_index, d.other_index) for d in overlaps}
    assert pairs == {(0, 1), (1, 2), (2, 3)}
    first = overlaps[0]
    assert first.detail["conflict_ms"] == 500
    assert first.detail["required_gap_ms"] == 1


def test_same_start_distinguished_from_overlap():
    _, diags = diagnose_fixture("same_start.vtt")
    same = [d for d in diags if d.code == "same_start"]
    assert len(same) == 1
    assert (same[0].cue_index, same[0].other_index) == (0, 1)
    # and the identical-start pair is also an overlap
    assert any(d.code == "overlap" and d.cue_index == 0 for d in diags)


def test_negative_zero_and_short_durations():
    _, diags = diagnose_fixture("negative_duration.srt")
    by_code = {}
    for d in diags:
        by_code.setdefault(d.code, []).append(d)
    assert by_code["negative_duration"][0].cue_index == 0
    assert by_code["negative_duration"][0].detail["duration_ms"] == -3000
    assert by_code["zero_duration"][0].cue_index == 2
    short = by_code["too_short"][0]
    assert short.cue_index == 1 and short.detail["duration_ms"] == 500


def test_crosses_boundary_specific_segment():
    _, diags = diagnose_fixture("multilingual.srt")
    cross = [d for d in diags if d.code == "crosses_boundary"]
    assert len(cross) == 1
    assert cross[0].cue_index == 5
    assert cross[0].detail["boundary_ms"] == 60_000


def test_too_long_diagnostic():
    from app.core.models import Cue
    cues = [Cue(0, 0, 8_000, ("long one",))]
    diags = diagnose(cues, **SETTINGS_KW)
    assert [d.code for d in diags] == ["too_long"]
    assert diags[0].detail["duration_ms"] == 8_000


def test_clean_fixture_has_no_timing_diagnostics():
    _, diags = diagnose_fixture("clean.srt")
    timing_codes = {d.code for d in diags}
    assert timing_codes == set()


def test_horizon_and_negative_start_errors():
    from app.core.models import Cue
    cues = [
        Cue(0, -5, 1000, ("before zero",)),
        Cue(1, 89_000, 91_000, ("past horizon",)),
    ]
    diags = diagnose(cues, **SETTINGS_KW)
    codes = {d.code for d in diags}
    assert "starts_before_zero" in codes
    assert "ends_past_horizon" in codes
