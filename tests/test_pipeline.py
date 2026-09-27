"""End-to-end pipeline tests against fixtures with known ground truth.

Ground truth (offset, drift, drop/spurious locations) comes from the fixture
*specification*, which is independent of the estimation code under test.
"""

import json
from pathlib import Path

import pytest

from driftcorr.media.metadata import load_metadata
from driftcorr.media.wav_io import read_wav
from driftcorr.pipeline import (
    InsufficientEvidence, NoSyncPoints, run_pipeline,
)

TRUE_OFFSET_S = 0.123
TRUE_DRIFT_PPM = 75.0


def _run(scn, cfg, job_id="testjob"):
    return run_pipeline(
        job_id=job_id, request_id="test-request",
        reference=read_wav(scn["reference_wav"]),
        target=read_wav(scn["target_wav"]),
        reference_meta=load_metadata(scn["reference_meta"]),
        target_meta=load_metadata(scn["target_meta"]),
        cfg=cfg,
    )


def test_clean_fixture_recovers_ground_truth(scenarios, cfg):
    rep = _run(scenarios["clean"], cfg, "clean-job")
    assert rep["status"] == "ok"
    est = rep["estimate"]
    assert est["offset_s"] == pytest.approx(TRUE_OFFSET_S, abs=2e-3)
    assert est["drift_ppm"] == pytest.approx(TRUE_DRIFT_PPM, abs=5.0)
    assert est["n_outliers"] == 0
    assert est["residual_rms_s"] < 2e-3
    # Usable interval spans the declared pulse range.
    assert est["usable_interval_s"]["start"] == pytest.approx(0.5, abs=1e-9)
    assert est["usable_interval_s"]["end"] == pytest.approx(7.5, abs=1e-9)
    # Post-correction alignment residual must be small.
    ac = rep["alignment_check"]
    assert ac["residual_rms_ms"] < 2.0
    assert ac["residual_max_ms"] < 5.0
    # Corrected audio exists and is roughly reference-length.
    out = Path(rep["corrected_audio"]["path"])
    assert out.exists()
    assert 7.0 < rep["corrected_audio"]["duration_s"] < 8.5
    # Correlation is never absolute-time proof; the report must say so.
    assert rep["absolute_time_verified"] is False
    assert "absolute" in rep["absolute_time_note"]


def test_corrupted_fixture_outliers_flagged_truth_still_recovered(scenarios, cfg):
    rep = _run(scenarios["corrupted"], cfg, "corrupted-job")
    assert rep["status"] == "ok"
    est = rep["estimate"]
    # Robust fit must still recover the true clock parameters.
    assert est["offset_s"] == pytest.approx(TRUE_OFFSET_S, abs=2e-3)
    assert est["drift_ppm"] == pytest.approx(TRUE_DRIFT_PPM, abs=5.0)
    # Exactly the two corrupted sync points must be flagged:
    #   ref 3.5 s -> spurious pulse planted at target 3.55 s
    #   ref 7.5 s -> 25 ms of frames dropped at target 7.32 s
    outliers = [p["ref_time_s"] for p in est["sync_points"] if not p["inlier"]]
    assert sorted(outliers) == [3.5, 7.5]
    assert est["n_inliers"] == 6


def test_degenerate_fixture_refuses_to_correct(scenarios, cfg):
    with pytest.raises(InsufficientEvidence) as excinfo:
        _run(scenarios["degenerate"], cfg, "degenerate-job")
    assert excinfo.value.error_class == "insufficient_evidence"


def test_pulseless_target_raises_no_sync_points(scenarios, cfg, tmp_path):
    import numpy as np

    from driftcorr.media.wav_io import write_wav
    silent = tmp_path / "silent.wav"
    write_wav(silent, np.zeros(8000), 8000)
    with pytest.raises(NoSyncPoints) as excinfo:
        run_pipeline(
            job_id="silent-job", request_id="test-request",
            reference=read_wav(scenarios["clean"]["reference_wav"]),
            target=read_wav(silent),
            reference_meta=load_metadata(scenarios["clean"]["reference_meta"]),
            target_meta=None,
            cfg=cfg,
        )
    assert excinfo.value.error_class == "no_sync_points"


def test_metadata_mapping_is_separate_from_audio(scenarios, cfg):
    rep = _run(scenarios["clean"], cfg, "mapping-job")
    tm = rep["time_mapping"]
    assert tm["offset_s"] == pytest.approx(TRUE_OFFSET_S, abs=2e-3)
    assert "events" in tm
    # Events were planted at target-time = offset + (1+drift)*t_ref for
    # t_ref in {1.0, 3.0, 7.0}; mapping must recover those reference times.
    got = {e["name"]: e for e in tm["events"]}
    for name, t_ref in (("segment_start", 1.0), ("annotation_a", 3.0),
                        ("segment_end", 7.0)):
        assert got[name]["ref_time_s"] == pytest.approx(t_ref, abs=5e-3)
        assert got[name]["within_usable_interval"]
    # The mapping output must not depend on the corrected audio block.
    assert set(tm) != set(rep["corrected_audio"])


def test_report_records_inputs_and_version(scenarios, cfg):
    rep = _run(scenarios["clean"], cfg, "audit-job")
    assert rep["job_id"] == "audit-job"
    assert rep["request_id"] == "test-request"
    assert rep["pipeline_version"].startswith("driftcorr-pipeline/")
    assert rep["inputs"]["reference"]["sha256_16"]
    assert rep["inputs"]["target"]["duration_s"] > 0
    # JSON-serializable (it will be stored in SQLite).
    json.dumps(rep)
