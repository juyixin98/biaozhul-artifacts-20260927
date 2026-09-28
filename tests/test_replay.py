"""Offline replay + full-rebuild consistency tests."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reorgindex.replay.rebuild import rebuild_file  # noqa: E402
from reorgindex.replay.verify import verify_rebuild_consistency  # noqa: E402
from tests.oracle.reference import expected_projection  # noqa: E402

pytestmark = pytest.mark.replay


def _replay(tmp_path: Path, scenario: str, *, inject_crash: bool = True) -> dict:
    recording = json.loads((ROOT / "fixtures" / scenario / "recording.json").read_text())
    return rebuild_file(
        ROOT / "fixtures" / scenario / "recording.json",
        tmp_path / f"{scenario}.db",
        allowed_difficulties={4, 16},
        finality_depth=int(recording["finality_depth"]),
        authorized_producers={recording["producer_address"]},
    )


def test_replay_short_fork_report(tmp_path):
    report = _replay(tmp_path, "short_fork")
    names = [d["name"] for d in report["decisions"]]
    assert names == ["g0", "m1", "m2", "m3", "o1", "f1", "f2"]
    outcomes = {d["name"]: d["outcome"] for d in report["decisions"]}
    assert outcomes["o1"] == "PENDING"
    assert outcomes["f1"] == "ACCEPT_SWITCH"
    # rollback interval present
    f1 = next(d for d in report["decisions"] if d["name"] == "f1")
    assert f1["switch"]["rollback_height_range"] == [1, 3]
    assert report["pending_count"] == 0
    assert isinstance(report["accounts"], dict)


def test_replay_deep_fork_rejection_recorded(tmp_path):
    report = _replay(tmp_path, "deep_fork")
    d2 = next(d for d in report["decisions"] if d["name"] == "d2")
    assert d2["outcome"] == "REJECTED"
    assert d2["reason"] == "REORG_FINALIZED"
    assert d2["switch"]["rollback_heights"] == [1, 5]


def test_replay_interrupted_switch_is_resumed(tmp_path):
    report = _replay(tmp_path, "interrupt")
    f1 = next(d for d in report["decisions"] if d["name"] == "f1")
    assert f1["outcome"] == "ACCEPT_SWITCH_RESUMED"
    assert report["active_height"] == 2


def test_full_rebuild_matches_live_index(tmp_path):
    """Derived result == full rebuild of the same fixture into a fresh DB."""
    scenario = "short_fork"
    recording = json.loads((ROOT / "fixtures" / scenario / "recording.json").read_text())
    live_db = tmp_path / "live.db"
    rebuild_file(
        ROOT / "fixtures" / scenario / "recording.json",
        live_db,
        allowed_difficulties={4, 16},
        finality_depth=int(recording["finality_depth"]),
        authorized_producers={recording["producer_address"]},
    )
    result = verify_rebuild_consistency(
        ROOT / "fixtures" / scenario / "recording.json",
        live_db,
        tmp_path / "rebuilt.db",
        allowed_difficulties={4, 16},
        finality_depth=int(recording["finality_depth"]),
        authorized_producers={recording["producer_address"]},
    )
    assert result["consistent"] is True
    assert result["live_active_tip"] == result["rebuilt_active_tip"]
    assert result["event_counts"]["live"] == result["event_counts"]["rebuilt"]


def test_rebuild_projection_matches_oracle(tmp_path):
    for scenario, tip in (("short_fork", "o1"), ("deep_fork", "m5"), ("interrupt", "f2")):
        recording = json.loads(
            (ROOT / "fixtures" / scenario / "recording.json").read_text()
        )
        db = tmp_path / f"{scenario}-o.db"
        report = rebuild_file(
            ROOT / "fixtures" / scenario / "recording.json",
            db,
            allowed_difficulties={4, 16},
            finality_depth=int(recording["finality_depth"]),
            authorized_producers={recording["producer_address"]},
        )
        oracle = expected_projection(recording, tip_name=tip)
        assert report["active_hashes"] == oracle["chain_hashes"]
        assert set(report["accounts"]) == set(oracle["balances"])
        for addr, balance in oracle["balances"].items():
            assert report["accounts"][addr]["balance"] == balance
        # event cardinality: oracle events are tx-level; store events include
        # paired debit/credit rows, so compare the summed deltas per address.
        sums: dict[str, int] = {}
        for row in report["derived_events"]:
            sums[row["address"]] = sums.get(row["address"], 0) + int(row["amount_delta"])
        assert sums == oracle["balances"]
