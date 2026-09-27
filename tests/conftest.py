"""Shared pytest fixtures: synthetic audio generated in a tmp directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from driftcorr.config import AppConfig
from driftcorr.fixtures.synth import (
    DropSpec, FixtureSpec, SpuriousPulse, generate_fixture,
    reference_metadata, target_metadata,
)
from driftcorr.media.wav_io import write_wav

TRUE_OFFSET_S = 0.123
TRUE_DRIFT_PPM = 75.0


def _write_scenario(root: Path, name: str, spec: FixtureSpec) -> dict:
    fx = generate_fixture(spec)
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    write_wav(d / "reference.wav", fx.reference, fx.fs)
    write_wav(d / "target.wav", fx.target, fx.fs)
    (d / "reference.meta.json").write_text(json.dumps(reference_metadata(spec)))
    (d / "target.meta.json").write_text(json.dumps(target_metadata(spec)))
    (d / "ground_truth.json").write_text(json.dumps(fx.ground_truth))
    return {
        "dir": d,
        "spec": spec,
        "reference_wav": str(d / "reference.wav"),
        "target_wav": str(d / "target.wav"),
        "reference_meta": str(d / "reference.meta.json"),
        "target_meta": str(d / "target.meta.json"),
        "ground_truth": fx.ground_truth,
    }


@pytest.fixture(scope="session")
def scenarios(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("scenarios")
    return {
        "clean": _write_scenario(root, "clean", FixtureSpec()),
        "corrupted": _write_scenario(root, "corrupted", FixtureSpec(
            drops=(DropSpec(at_target_time_s=7.32, duration_s=0.025),),
            spurious=(SpuriousPulse(at_target_time_s=3.55, amplitude=1.4),),
        )),
        "degenerate": _write_scenario(root, "degenerate",
                                      FixtureSpec(pulse_times_s=(0.5, 7.5))),
    }


@pytest.fixture()
def cfg(tmp_path) -> AppConfig:
    return AppConfig(
        db_path=str(tmp_path / "jobs.sqlite3"),
        output_dir=str(tmp_path / "outputs"),
    )
