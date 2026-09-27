"""Tests for the *independent* fixture generator itself.

These guard the integrity of the test oracle: the generator must not import
the package core, must write internally consistent stereo WAVs, and its truth
must be computed from construction parameters.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))

import fixturegen  # noqa: E402
from fixturegen import SCENARIOS, generate  # noqa: E402



def test_generator_does_not_import_core():
    import ast
    import inspect

    def imported_roots(path):
        tree = ast.parse(Path(path).read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    gen_file = inspect.getfile(fixturegen)
    for py in (gen_file,
               REPO_ROOT / "tools" / "fixturegen" / "media_util.py",
               REPO_ROOT / "tools" / "fixturegen" / "cli.py"):
        assert "clockalign" not in imported_roots(py), (
            f"{py} imports the package core; the oracle must be independent")


def test_generate_writes_wav_and_truth(tmp_path):
    index = generate(tmp_path, names=("drift_offset",))
    assert "drift_offset" in index
    wav = Path(index["drift_offset"]["wav"])
    truth_path = Path(index["drift_offset"]["truth"])
    assert wav.exists() and truth_path.exists()

    import wave
    with wave.open(str(wav), "rb") as wf:
        assert wf.getnchannels() == 2
        assert wf.getframerate() == 16000
        assert wf.getsampwidth() == 2

    truth = json.loads(truth_path.read_text())
    # Truth is internally consistent with the construction parameters.
    sc = SCENARIOS["drift_offset"]
    assert truth["drift_ppm"] == sc.drift_ppm
    assert truth["offset_s"] == sc.offset_s
    assert truth["slope"] == pytest.approx(1 + sc.drift_ppm * 1e-6)
    assert len(truth["pulse_times_reality_s"]) == len(sc.pulse_times)
    assert truth["generated_by"].startswith("tools.fixturegen")


def test_dropped_frame_fixture_records_cut_and_samples(tmp_path):
    generate(tmp_path, names=("dropped_frames",))
    truth = json.loads((tmp_path / "dropped_frames.truth.json").read_text())
    assert truth["drop_times_s"] == [7.0]
    assert truth["drops"][0]["removed_samples"] == 160


def test_bad_sync_fixture_includes_only_slave_pulses(tmp_path):
    generate(tmp_path, names=("bad_sync",))
    truth = json.loads((tmp_path / "bad_sync.truth.json").read_text())
    assert truth["spurious_slave_pulse_times_s"] == [4.25, 8.75]
    # Real pulse truth still lists the genuine ones.
    assert len(truth["pulse_times_reality_s"]) == 7
