"""Unit tests for the independent validation checks and failure categories."""
from __future__ import annotations

import pytest

from clockalign.validation import (check_drift, check_drop_times,
                                    check_offset, check_residuals,
                                    validate_report)


def _cfg():
    from clockalign.config import load_config
    return load_config().validation


def _report(ppm, off, rms=0.0005, drops=()):
    return {
        "estimate": {"drift_ppm": ppm, "offset_s": off},
        "residual_report": {
            "global": {"rms_residual_s": rms, "max_abs_residual_s": rms},
            "usable_reference_range_s": [2.0, 11.0],
            "segments": [{"segment_index": 0, "rms_residual_s": rms}],
            "frame_gaps": [
                {"kind": "frame_drop", "reference_time_start_s": t,
                 "reference_time_end_s": t + 0.01} for t in drops],
        },
    }


def test_residual_check_pass_and_fail():
    cfg = _cfg()
    ok = check_residuals(_report(100, 0.1, rms=0.0001), cfg)
    bad = check_residuals(_report(100, 0.1, rms=0.01), cfg)
    assert ok.passed and ok.category == "ok"
    assert not bad.passed and bad.category == "residual_too_large"


def test_drift_check_reports_specific_error():
    cfg = _cfg()
    check = check_drift(_report(120.0, 0.25), {"drift_ppm": 120.0}, cfg)
    assert check.passed
    check = check_drift(_report(120.0, 0.25), {"drift_ppm": 200.0}, cfg)
    assert not check.passed
    assert check.category == "drift_out_of_tolerance"
    assert check.detail["abs_error_ppm"] == 80.0


def test_offset_check_reports_specific_error():
    cfg = _cfg()
    check = check_offset(_report(120.0, 0.250), {"offset_s": 0.250}, cfg)
    assert check.passed
    check = check_offset(_report(120.0, 0.300), {"offset_s": 0.250}, cfg)
    assert check.category == "offset_out_of_tolerance"
    assert check.detail["abs_error_ms"] == pytest.approx(50.0)


def test_drop_time_check_including_false_alarm_category():
    cfg = _cfg()
    check = check_drop_times(_report(80, -0.12, drops=(7.01,)),
                             {"drop_times_s": [7.0]}, cfg)
    assert check.passed, check.detail
    check = check_drop_times(_report(80, -0.12, drops=()),
                             {"drop_times_s": [7.0]}, cfg)
    assert check.category == "drop_count_mismatch"
    # Report claims a drop the truth does not have.
    check = check_drop_times(_report(80, -0.12, drops=(4.0,)),
                             {"drop_times_s": []}, cfg)
    assert not check.passed and check.category == "unexpected_frame_drop"


def test_validate_report_aggregates_categories():
    cfg = _cfg()
    result = validate_report(
        _report(500.0, 0.9, rms=0.02), cfg,
        truth={"drift_ppm": 100.0, "offset_s": 0.1})
    assert not result["passed"]
    cats = set(result["failure_categories"])
    assert "drift_out_of_tolerance" in cats
    assert "offset_out_of_tolerance" in cats
    assert "residual_too_large" in cats
