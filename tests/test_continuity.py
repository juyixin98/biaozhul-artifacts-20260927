"""Per-PID continuity-counter contract tests.

These assert the *specific* verdict category (duplicate accepted, unsignaled
gap rejected, signaled discontinuity accepted, adaptation-only non-increment)
rather than merely that the checker returned something.
"""
from __future__ import annotations

from app.core.continuity import ContinuityChecker
from app.core.sync import parse_packet
from tools.tsbuilder import ts_packet


def _check(cc: ContinuityChecker, raw: bytes, index: int = 0):
    return cc.check(parse_packet(raw, index, 0))


def test_payload_packets_increment_counter():
    cc = ContinuityChecker()
    p0 = _check(cc, ts_packet(0x1100, b"a" * 10, cc=0))
    p1 = _check(cc, ts_packet(0x1100, b"b" * 10, cc=1), 1)
    p2 = _check(cc, ts_packet(0x1100, b"c" * 10, cc=2), 2)
    assert p0.kind == "baseline"
    assert p1.kind == "ok"
    assert p2.kind == "ok"
    assert p1.finding is None and p2.finding is None


def test_adaptation_only_does_not_increment_and_must_repeat_cc():
    cc = ContinuityChecker()
    _check(cc, ts_packet(0x1100, b"a" * 10, cc=0))
    # adaptation-only repeats cc=0 -> legal
    repeat = _check(cc, ts_packet(0x1100, b"", cc=0, random_access=True), 1)
    assert repeat.kind == "adaptation_repeat"
    assert repeat.finding is None
    # following payload advances to cc=1
    nxt = _check(cc, ts_packet(0x1100, b"b" * 10, cc=1), 2)
    assert nxt.kind == "ok"


def test_adaptation_only_with_changed_cc_is_rejected():
    cc = ContinuityChecker()
    _check(cc, ts_packet(0x1100, b"a" * 10, cc=0))
    bad = _check(cc, ts_packet(0x1100, b"", cc=1), 1)
    assert bad.kind == "adaptation_cc_mismatch"
    assert bad.finding is not None
    assert bad.finding.code == "adaptation_only_cc_mismatch"


def test_exact_duplicate_payload_is_accepted():
    cc = ContinuityChecker()
    original = ts_packet(0x1100, b"same" * 40, cc=5)
    _check(cc, original, 0)
    dup = _check(cc, original, 1)
    assert dup.kind == "duplicate"
    assert dup.finding.code == "duplicate_packet"
    assert dup.finding.disposition.value == "accepted"
    cc.note_bytes_equal(dup, True)
    assert dup.kind == "duplicate"


def test_same_cc_different_bytes_is_a_distinct_failure():
    cc = ContinuityChecker()
    _check(cc, ts_packet(0x1100, b"aaaa" * 40, cc=5), 0)
    changed = _check(cc, ts_packet(0x1100, b"bbbb" * 40, cc=5), 1)
    assert changed.kind == "duplicate"
    cc.note_bytes_equal(changed, False)
    assert changed.kind == "repeat_payload_without_duplicate_bit"
    assert changed.finding.code == "cc_repeat_payload_without_duplicate_bit"
    assert changed.finding.disposition.value == "rejected"


def test_unsignaled_gap_is_rejected_with_missing_estimate():
    cc = ContinuityChecker()
    _check(cc, ts_packet(0x1100, b"a" * 10, cc=0), 0)
    _check(cc, ts_packet(0x1100, b"b" * 10, cc=1), 1)
    # cc jumps 1 -> 5: delta 4, i.e. 3 packets lost
    gap = _check(cc, ts_packet(0x1100, b"c" * 10, cc=5), 2)
    assert gap.kind == "gap"
    assert gap.missing_packets == 3
    assert gap.finding.code == "cc_gap"
    assert gap.finding.disposition.value == "rejected"


def test_signaled_discontinuity_is_accepted_and_distinct_from_loss():
    cc = ContinuityChecker()
    _check(cc, ts_packet(0x1100, b"a" * 10, cc=0), 0)
    _check(cc, ts_packet(0x1100, b"", cc=0, discontinuity=True), 1)
    # First payload after the DI restarts the counter: any CC accepted.
    restart = _check(cc, ts_packet(0x1100, b"b" * 10, cc=9), 2)
    assert restart.kind == "signaled_discontinuity"
    assert restart.signaled is True
    assert restart.finding.code == "signaled_discontinuity"
    assert restart.finding.disposition.value == "accepted"


def test_discontinuity_flag_with_continuous_counter_is_flag_no_gap():
    cc = ContinuityChecker()
    _check(cc, ts_packet(0x1100, b"a" * 10, cc=0, discontinuity=True), 0)
    # counter still advances normally cc0 -> cc1, DI set but nothing lost
    nxt = _check(cc, ts_packet(0x1100, b"b" * 10, cc=1,
                               discontinuity=True), 1)
    assert nxt.kind == "signaled_discontinuity"
    assert nxt.missing_packets == 0


def test_pids_are_checked_independently():
    cc = ContinuityChecker()
    _check(cc, ts_packet(0x1100, b"a", cc=0), 0)
    _check(cc, ts_packet(0x200, b"b", cc=7), 1)
    # PID 0x1100 jumps 0->8 (gap) but PID 0x200 simply advances 7->8 (ok)
    gap = _check(cc, ts_packet(0x1100, b"c", cc=8), 2)
    ok = _check(cc, ts_packet(0x200, b"d", cc=8), 3)
    assert gap.kind == "gap"
    assert ok.kind == "ok"


def test_null_pid_is_not_checked():
    cc = ContinuityChecker()
    v = _check(cc, ts_packet(0x1FFF, b"\xff" * 184, cc=0))
    assert v.kind == "null_pid"
