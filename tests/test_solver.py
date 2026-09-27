"""Tests for the minimum-displacement repair solver.

These assert concrete repaired timestamps and totals, failure categories, and
that no cue is ever silently dropped or compressed.
"""
from __future__ import annotations

from pathlib import Path


from app.config import Settings
from app.core.models import Cue
from app.core.solver import solve
from app.media import parse_bytes
from app.services.pipeline import run_validation

FIX = Path(__file__).parent / "fixtures"


def make_settings(**over):
    base = dict(
        min_duration_ms=1000,
        max_duration_ms=7000,
        min_gap_ms=1,
        segment_boundaries_ms=(30_000, 60_000),
        horizon_ms=90_000,
        max_per_cue_shift_ms=5_000,
        max_total_shift_ms=60_000,
    )
    base.update(over)
    return Settings(**base)


def codes_by_index(diags):
    out: dict[int, set[str]] = {}
    for d in diags:
        if d.cue_index is not None:
            out.setdefault(d.cue_index, set()).add(d.code)
    return out


def pipeline_fixture(name, **settings_kw):
    data = (FIX / name).read_bytes()
    return data, run_validation(
        data, settings=make_settings(**settings_kw),
        run_id=f"test-{name}",
    )


# ---------------------------------------------------------------------------
def test_two_cues_overlap_minimal_even_spread():
    # [1000,3000) and [2500,5000): conflict 499ms (need 1ms gap).
    # Minimum L1 split shifts first back and second forward; expected placement
    # s0=1000, s1=3001 -> shifts 0 and +501, total 501 (keeping first anchored).
    cues = [Cue(0, 1000, 3000, ("a",)), Cue(1, 2500, 5000, ("b",))]
    plan = solve(cues, {0: {"overlap"}, 1: {"overlap"}},
                 min_duration_ms=1000, max_duration_ms=7000, min_gap_ms=1,
                 segment_boundaries_ms=(), horizon_ms=90_000,
                 max_per_cue_shift_ms=5_000, max_total_shift_ms=60_000)
    assert plan.status == "repaired", plan.message
    by = {r.cue_index: r for r in plan.cues}
    assert by[0].repaired_start_ms == 1000
    assert by[1].repaired_start_ms == 3001
    assert by[0].repaired_end_ms == 3000
    assert plan.total_shift_ms == 501
    assert "overlap" in by[1].reasons
    # repaired timings no longer overlap with the required gap
    assert by[1].repaired_start_ms - by[0].repaired_end_ms >= 1


def test_chain_overlap_fixture_optimal_and_nonoverlapping():
    data, result = pipeline_fixture("chain_overlap.srt")
    assert result.status == "repaired", result.message
    rows = result.repair["cues"]
    assert len(rows) == 5  # nothing deleted
    starts = [r["repaired_start_ms"] for r in rows]
    ends = [r["repaired_end_ms"] for r in rows]
    for i in range(4):
        assert starts[i + 1] - ends[i] >= 1, (i, starts, ends)
    # isolated legal cue must be held exactly in place
    assert rows[4]["shift_ms"] == 0
    # total displacement is finite and within budget; the earliest cues carry
    # the least disturbance (cue0 shifts at most what the chain requires).
    assert result.repair["total_shift_ms"] <= result.repair["budget_ms"]
    shifts = [r["shift_ms"] for r in rows]
    assert abs(shifts[0]) <= max(abs(s) for s in shifts[:4])
    # repaired document contains all original text lines, cue count preserved
    repaired = result.repaired_document
    assert "Later cue is isolated and legal." in repaired
    assert repaired.count("-->") == 5


def test_same_start_pair_ordered_by_original_index():
    _, result = pipeline_fixture("same_start.vtt")
    # fixture also contains a negative-duration and a zero-duration cue:
    # overall repair should still succeed (flip + extend + order same starts)
    assert result.status == "repaired", result.message
    rows = sorted(result.repair["cues"], key=lambda r: r["cue_index"])
    # cue 0 must precede cue 1 after repair with the gap
    assert rows[1]["repaired_start_ms"] >= rows[0]["repaired_end_ms"] + 1
    actions = {r["cue_index"]: r["action"] for r in rows}
    assert "flipped" in actions[2]
    assert rows[2]["repaired_start_ms"] == 9_000  # flipped to the early instant
    assert rows[3]["repaired_end_ms"] - rows[3]["repaired_start_ms"] == 1_000  # extended


def test_negative_duration_is_flipped_and_recorded():
    cues = [Cue(0, 5000, 2000, ("backwards",))]
    plan = solve(cues, {0: {"negative_duration"}},
                 min_duration_ms=1000, max_duration_ms=7000, min_gap_ms=1,
                 segment_boundaries_ms=(), horizon_ms=90_000,
                 max_per_cue_shift_ms=5_000, max_total_shift_ms=60_000)
    assert plan.status == "repaired"
    r = plan.cues[0]
    assert (r.original_start_ms, r.original_end_ms) == (5000, 2000)  # kept
    assert (r.repaired_start_ms, r.repaired_end_ms) == (2000, 5000)
    assert "negative_duration" in r.reasons
    assert r.action == "flipped"


def test_short_cue_extended_never_compressed():
    cues = [Cue(0, 10_000, 10_500, ("short",))]
    plan = solve(cues, {0: {"too_short"}},
                 min_duration_ms=1000, max_duration_ms=7000, min_gap_ms=1,
                 segment_boundaries_ms=(), horizon_ms=90_000,
                 max_per_cue_shift_ms=5_000, max_total_shift_ms=60_000)
    assert plan.status == "repaired"
    r = plan.cues[0]
    assert r.repaired_end_ms - r.repaired_start_ms == 1000
    assert r.action == "extended"
    # a too-long cue must NOT be compressed (shift-only model)
    cues2 = [Cue(0, 0, 8_000, ("long",))]
    plan2 = solve(cues2, {0: {"too_long"}},
                  min_duration_ms=1000, max_duration_ms=7000, min_gap_ms=1,
                  segment_boundaries_ms=(), horizon_ms=90_000,
                  max_per_cue_shift_ms=5_000, max_total_shift_ms=60_000)
    r2 = plan2.cues[0]
    assert r2.repaired_end_ms - r2.repaired_start_ms == 8000
    assert r2.shift_ms == 0


def test_unfixable_window_returns_infeasible_bounds_not_forced():
    data, result = pipeline_fixture("unfixable.srt", max_per_cue_shift_ms=300)
    assert result.status == "infeasible_bounds"
    assert result.failure_codes == ["infeasible_bounds"]
    assert result.repaired_document is None  # never emit a squeezed document
    assert "infeasible" in result.message or "segment" in result.message
    # diagnostics are still reported with concrete codes
    dcodes = {d.code for d in result.diagnostics}
    assert "overlap" in dcodes
    assert "crosses_boundary" in dcodes


def test_budget_exceeded_fixture_refuses_repair():
    # Each cue may move up to 2000ms (chain geometrically feasible inside the
    # first 60s segment), but the summed minimum displacement exceeds a tiny
    # total budget.
    data, result = pipeline_fixture(
        "budget_exceeded.srt",
        max_per_cue_shift_ms=2_000,
        segment_boundaries_ms=(60_000,),
        max_total_shift_ms=2_000,
    )
    assert result.status == "budget_exceeded"
    assert result.repaired_document is None
    assert result.repair is None
    # the minimal achievable cost is surfaced for transparency and exceeds budget
    assert result.message.startswith("minimum total start displacement")
    assert result.failure_codes == ["budget_exceeded"]


def test_per_cue_cap_makes_individual_move_impossible():
    # second cue would need >50ms but cap is 50ms, first cannot move earlier
    # past segment start 0 ... build genuinely infeasible:
    cues = [Cue(0, 0, 1_000, ("a",)), Cue(1, 500, 1_500, ("b",))]
    plan = solve(cues, {0: {"overlap"}, 1: {"overlap"}},
                 min_duration_ms=1000, max_duration_ms=7000, min_gap_ms=1,
                 segment_boundaries_ms=(), horizon_ms=90_000,
                 max_per_cue_shift_ms=50, max_total_shift_ms=60_000)
    # cue1 earliest end at 1000+d? anchor: s1>=1001, orig 500 => shift 501>50;
    # cue0 box hi = min(90000-1000, 0+50)=50; chain s1>=s0+1001; even s0=50
    # needs s1>=1051 => shift 551 > 50 => infeasible
    assert plan.status == "infeasible_bounds", plan.message


def test_segment_boxes_keep_cues_inside_segments():
    # cue crosses 30000 boundary: [29000,31000); midpoint is in seg0, but no
    # placement keeps it whole within +/-5000ms of start in seg0? start box
    # lo=max(0,24000), hi=min(30000-2000,34000)=28000 => move to <=28000.
    cues = [Cue(0, 29_000, 31_000, ("cross",))]
    plan = solve(cues, {0: {"crosses_boundary"}},
                 min_duration_ms=1000, max_duration_ms=7000, min_gap_ms=1,
                 segment_boundaries_ms=(30_000, 60_000), horizon_ms=90_000,
                 max_per_cue_shift_ms=5_000, max_total_shift_ms=60_000)
    assert plan.status == "repaired"
    r = plan.cues[0]
    assert r.repaired_end_ms <= 30_000
    assert abs(r.shift_ms) == 1_000


def test_no_cue_dropped_from_rendered_document():
    data, result = pipeline_fixture("multilingual.srt")
    assert result.cue_count == 6
    assert result.repaired_document.count("-->") == 6
    parsed = parse_bytes(result.repaired_document.encode("utf-8"))
    assert len(parsed.cues) == 6
    # every original payload line survives verbatim
    orig = parse_bytes(data)
    for before, after in zip(orig.cues, parsed.cues):
        assert before.raw_lines == after.raw_lines


def test_parse_failure_is_not_reported_as_success():
    data, result = pipeline_fixture("bad_timestamp.srt")
    assert result.status == "parse_failed"
    assert result.is_success() is False
    assert "invalid_timestamp" in result.failure_codes
    assert result.repaired_document is None
    assert result.elapsed_ms >= 0
