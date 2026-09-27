"""Independent validation checks.

Two kinds of checks, kept separate from the estimator they judge:

1. *self-consistency*: post-correction residuals must be below a configured
   bound over the reported usable interval;
2. *ground-truth*: when independently generated fixture truth is supplied
   (drift ppm, offset, drop times), recovered parameters must lie within
   explicit tolerances. The truth is produced by the standalone fixture
   generator -- never by this package's core.

Each check returns a structured pass/fail with a category so tests can assert
the specific failure mode rather than merely "endpoint answered".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ValidationCfg


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    category: str
    detail: dict[str, Any]

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "category": self.category, "detail": self.detail}


def check_residuals(report: dict, cfg: ValidationCfg) -> Check:
    residual = report.get("residual_report") or {}
    global_r = residual.get("global") or {}
    rms = global_r.get("rms_residual_s")
    mx = global_r.get("max_abs_residual_s")
    usable = residual.get("usable_reference_range_s")
    if rms is None or mx is None or usable is None:
        return Check("residuals_within_bounds", False,
                     "no_residual_report",
                     {"message": "result carries no residual report"})
    seg_failures = []
    for seg in residual.get("segments", []):
        if seg["rms_residual_s"] > cfg.residual_rms_max_s:
            seg_failures.append({
                "segment_index": seg["segment_index"],
                "rms_residual_s": seg["rms_residual_s"],
                "bound_s": cfg.residual_rms_max_s})
    passed = (rms <= cfg.residual_rms_max_s and not seg_failures)
    return Check("residuals_within_bounds", passed,
                 "residual_too_large" if not passed else "ok",
                 {"rms_residual_s": rms, "max_abs_residual_s": mx,
                  "bound_rms_s": cfg.residual_rms_max_s,
                  "usable_reference_range_s": usable,
                  "segment_violations": seg_failures})


def check_drift(report: dict, truth: dict, cfg: ValidationCfg) -> Check:
    est = (report.get("estimate") or {}).get("drift_ppm")
    true = truth.get("drift_ppm")
    if est is None or true is None:
        return Check("drift_recovery", False, "missing_parameter",
                     {"estimated": est, "truth": true})
    err = abs(est - true)
    return Check("drift_recovery", err <= cfg.drift_ppm_tol,
                 "ok" if err <= cfg.drift_ppm_tol
                 else "drift_out_of_tolerance",
                 {"estimated_ppm": est, "truth_ppm": true,
                  "abs_error_ppm": err, "tolerance_ppm": cfg.drift_ppm_tol})


def check_offset(report: dict, truth: dict, cfg: ValidationCfg) -> Check:
    est = (report.get("estimate") or {}).get("offset_s")
    true = truth.get("offset_s")
    if est is None or true is None:
        return Check("offset_recovery", False, "missing_parameter",
                     {"estimated": est, "truth": true})
    err_ms = abs(est - true) * 1000.0
    return Check("offset_recovery", err_ms <= cfg.offset_ms_tol,
                 "ok" if err_ms <= cfg.offset_ms_tol
                 else "offset_out_of_tolerance",
                 {"estimated_ms": est * 1000.0, "truth_ms": true * 1000.0,
                  "abs_error_ms": err_ms, "tolerance_ms": cfg.offset_ms_tol})


def check_drop_times(report: dict, truth: dict, cfg: ValidationCfg) -> Check:
    true_drops = sorted(float(t) for t in truth.get("drop_times_s", []))
    gaps = [g for g in (report.get("residual_report") or {})
            .get("frame_gaps", []) if g["kind"] == "frame_drop"]
    found = sorted(float(g["reference_time_start_s"]) for g in gaps)
    if not true_drops:
        passed = not found
        return Check("drop_time_recovery", passed,
                     "unexpected_frame_drop" if not passed else "ok",
                     {"truth": true_drops, "found": found})
    if len(found) != len(true_drops):
        return Check("drop_time_recovery", False,
                     "drop_count_mismatch",
                     {"truth_n": len(true_drops), "found_n": len(found),
                      "truth_s": true_drops, "found_s": found})
    errors = [abs(f - t) for f, t in zip(found, true_drops)]
    worst = max(errors)
    ok = worst <= cfg.drop_time_tol_s
    return Check("drop_time_recovery", ok,
                 "ok" if ok else "drop_time_out_of_tolerance",
                 {"truth_s": true_drops, "found_s": found,
                  "abs_errors_s": errors, "worst_error_s": worst,
                  "tolerance_s": cfg.drop_time_tol_s})


def validate_report(report: dict, cfg: ValidationCfg,
                    truth: dict | None = None) -> dict:
    checks = [check_residuals(report, cfg)]
    if truth is not None:
        if "drift_ppm" in truth:
            checks.append(check_drift(report, truth, cfg))
        if "offset_s" in truth:
            checks.append(check_offset(report, truth, cfg))
        if "drop_times_s" in truth:
            checks.append(check_drop_times(report, truth, cfg))
    passed = all(c.passed for c in checks)
    return {"passed": passed,
            "checks": [c.as_dict() for c in checks],
            "failure_categories": [c.category for c in checks if not c.passed]}
