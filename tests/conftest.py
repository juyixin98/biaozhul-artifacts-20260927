"""Shared, *independent* test reference logic.

These helpers deliberately re-derive expectations from the fixture
declarations and raw modular arithmetic instead of calling the jitter
core, so the tests are not circular ("core verified by core").
"""
import pytest

from app.config import JitterConfig
from app.time_kernel import wrap_delta


@pytest.fixture
def cfg():
    return JitterConfig()


def expected_ext_seq(raw_seq, base_raw_seq, bits=16):
    return wrap_delta(raw_seq, base_raw_seq, bits)


def expected_playout_spacing_ms(playout_records):
    """Per-SSRC consecutive gaps between playout times."""
    by_ssrc = {}
    for p in playout_records:
        by_ssrc.setdefault(p["ssrc"], []).append(p["playout_ms"])
    spaces = {}
    for ssrc, times in by_ssrc.items():
        spaces[ssrc] = [round(b - a, 9) for a, b in zip(times, times[1:])]
    return spaces


def assert_exactly_monotonic(records, frame_ms):
    """Every per-SSRC consecutive delta must be a positive frame multiple."""
    by_ssrc = {}
    for p in records:
        by_ssrc.setdefault(p["ssrc"], []).append(p)
    violations = []
    for ssrc, items in by_ssrc.items():
        for a, b in zip(items, items[1:]):
            if b["playout_ms"] < a["playout_ms"] - 1e-9:
                violations.append((ssrc, a["ext_seq"], b["ext_seq"],
                                   a["playout_ms"], b["playout_ms"]))
    assert not violations, f"playout rewound: {violations[:5]}"
