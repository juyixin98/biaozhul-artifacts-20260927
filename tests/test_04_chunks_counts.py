"""Chunking invariance, exact sample counts, and very short streams."""

from __future__ import annotations

import numpy as np
import pytest

from resampler.signal import StreamingPolyphase, build_plan

RATIOS = [(16000, 48000), (48000, 16000), (48000, 44100),
          (44100, 48000), (11025, 48000), (22050, 8000)]
CHUNKS = [1, 2, 3, 5, 7, 13, 16, 31, 64, 100, 257, 10000]


def collect(plan, x, cuts):
    eng = StreamingPolyphase(plan)
    parts = []
    for a, b in zip(cuts[:-1], cuts[1:]):
        parts.append(eng.push(x[a:b]))
    parts.append(eng.flush())
    y = np.concatenate(parts)
    return y, eng


@pytest.mark.parametrize("rate_in,rate_out", RATIOS)
def test_bit_identical_output_across_chunkings(rate_in, rate_out, settings, runlog, rng):
    x = rng.standard_normal(401)
    plan = build_plan(rate_in, rate_out, settings=settings)
    ref_y, ref_eng = collect(plan, x, [0, len(x)])

    results = {len(x): ref_y}
    for c in CHUNKS:
        cuts = list(range(0, len(x), c)) + [len(x)]
        if cuts[0] != 0:
            cuts = [0] + cuts
        y, eng = collect(plan, x, cuts)
        results[c] = y
        runlog.check(f"chunk={c}: array_equal to one-shot",
                     np.array_equal(y, ref_y),
                     {"chunk": c, "len": y.size},
                     "state is only (K-1 history + next n): boundaries carry no info")
        runlog.check(f"chunk={c}: emitted counter consistent",
                     eng.outputs_emitted == y.size,
                     {"counter": eng.outputs_emitted, "len": y.size},
                     "monotonic output accounting")

    runlog.check("all chunkings share the expected count",
                 all(y.size == ref_y.size for y in results.values()),
                 {c: y.size for c, y in results.items()},
                 "count is a function of J only")


@pytest.mark.parametrize("rate_in,rate_out", RATIOS)
def test_output_count_formula(rate_in, rate_out, settings, runlog, rng):
    import math
    plan = build_plan(rate_in, rate_out, settings=settings)
    for J in [0, 1, 2, 3, 10, 100]:
        x = rng.standard_normal(J)
        eng = StreamingPolyphase(plan)
        y = eng.push(x)
        tail = eng.flush()
        total = y.size + tail.size
        expected = plan.expected_outputs(J)
        runlog.check(f"J={J} count formula", total == expected,
                     {"J": J, "got": total, "expected": expected},
                     "0 -> 0; J>=1 -> ceil(L*(J+K-1)/M)")
        if J >= 1:
            closed = math.ceil(plan.up * (J + plan.taps_per_phase - 1) / plan.down)
            runlog.check(f"J={J} closed form ceil(L*(J+K-1)/M)", total == closed,
                         {"closed": closed}, "documented formula")


def test_empty_stream_produces_nothing(settings, runlog):
    plan = build_plan(48000, 16000, settings=settings)
    eng = StreamingPolyphase(plan)
    a = eng.push(np.empty(0))
    b = eng.flush()
    runlog.check("empty push then flush -> zero outputs",
                 a.size == 0 and b.size == 0 and eng.outputs_emitted == 0,
                 {"a": a.size, "b": b.size}, "no invented samples")


def test_single_sample_stream(settings, runlog):
    """J=1 up 3x: q=floor(n/3) takes 0 for n=0..3K-1, so 3K outputs.

    Each of the K phases of the single input contributes at 3 output times.
    """
    plan = build_plan(16000, 48000, settings=settings)
    eng = StreamingPolyphase(plan)
    y = eng.push(np.array([0.5]))
    tail = eng.flush()
    all_y = np.concatenate([y, tail])
    runlog.check("J=1, L=3 -> 3K outputs", all_y.size == 3 * plan.taps_per_phase,
                 {"got": all_y.size, "3K": 3 * plan.taps_per_phase},
                 "ceil(3*(1+K-1)/1)=3K; q=floor(n/3) is 0 over all 3K outputs")


def test_constant_signal_settles_to_constant(settings, runlog):
    """DC input 1.0: interior outputs stay at 1.0 within truncation ripple.

    Global prototype normalization fixes system DC gain to exactly 1;
    per-phase column sums differ only by intrinsic sinc-truncation ripple.
    """
    plan = build_plan(48000, 44100, settings=settings)
    x = np.ones(3000)
    y = StreamingPolyphase(plan).push(x)  # before flush, interior covered
    interior = y[3 * plan.taps_per_phase:]
    err = float(np.max(np.abs(interior - 1.0)))
    runlog.check("DC input -> 1.0 in the interior, ripple <= 5e-5",
                 err <= 5e-5, {"max_err": err},
                 "ripple is bounded by per-column truncation variation")
    # Outputs alternate phases; all are finite and positive
    runlog.check("DC interior finite and in (0.999, 1.001)",
                 np.all(np.isfinite(interior)) and np.all(np.abs(interior - 1) < 1e-3),
                 {"min": float(interior.min()), "max": float(interior.max())},
                 "no phase produces a wild value")


def test_double_flush_is_state_conflict(settings):
    from resampler.errors import StateConflictError
    plan = build_plan(8000, 8000, settings=settings)
    eng = StreamingPolyphase(plan)
    eng.push(np.array([1.0, 2.0]))
    eng.flush()
    with pytest.raises(StateConflictError):
        eng.flush()


def test_push_after_flush_is_state_conflict(settings):
    from resampler.errors import StateConflictError
    plan = build_plan(8000, 8000, settings=settings)
    eng = StreamingPolyphase(plan)
    eng.flush()
    with pytest.raises(StateConflictError):
        eng.push(np.array([1.0]))
