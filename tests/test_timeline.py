"""Timeline mapping unit tests with literal expected values."""

import pytest

from driftcorr.core.timeline import TimeMapping, map_events
from driftcorr.media.metadata import TimedEvent


def test_mapping_uses_closed_form_not_first_timestamp_subtraction():
    # offset 100 ms, drift +100 ppm. Hand-computed:
    #   ref_to_target(10)  = 0.1 + 1.0001 * 10        = 10.101
    #   target_to_ref(10.101) = (10.101 - 0.1)/1.0001 = 10.0
    m = TimeMapping(offset_s=0.1, drift_ppm=100.0)
    assert m.ref_to_target(10.0) == pytest.approx(10.101, abs=1e-12)
    assert m.target_to_ref(10.101) == pytest.approx(10.0, abs=1e-9)
    # A naive "subtract the first timestamp" approach would give 10.001 here:
    assert m.target_to_ref(10.101) != pytest.approx(10.001, abs=1e-6)


def test_roundtrip():
    m = TimeMapping(offset_s=-0.25, drift_ppm=-43.0)
    for t in (0.0, 1.234, 97.5):
        assert m.ref_to_target(m.target_to_ref(t)) == pytest.approx(t, abs=1e-12)


def test_events_outside_usable_interval_are_flagged_not_dropped():
    m = TimeMapping(offset_s=0.1, drift_ppm=100.0)
    events = [TimedEvent("inside", 5.1505), TimedEvent("outside", 0.05)]
    mapped = map_events(events, m, usable_interval_s=(1.0, 8.0))
    assert len(mapped) == 2
    by_name = {e.name: e for e in mapped}
    # inside: (5.1505 - 0.1)/1.0001 = 5.05 (to within float rounding)
    assert by_name["inside"].ref_time_s == pytest.approx(5.05, abs=1e-5)
    assert by_name["inside"].within_usable_interval
    # outside: (0.05 - 0.1)/1.0001 < 1.0 -> mapped but flagged
    assert not by_name["outside"].within_usable_interval
