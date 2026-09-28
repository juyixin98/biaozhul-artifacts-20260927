"""Offline replay parsing tests."""

import json

import pytest

from lc.config import KernelConfig
from lc.errors import Category, Code, LightClientError
from lc.replay import load_bundle_file, parse_bundle


def _bundle(golden, vec_ids):
    items = []
    by_id = {v["id"]: v for v in golden["vectors"]}
    for vid in vec_ids:
        v = by_id[vid]
        if v["kind"] == "single":
            items.append(
                {
                    "header": v["header"],
                    "certificate": v["certificate"],
                    "next_committee": v.get("next_committee"),
                }
            )
    return {"items": items}


def test_parse_happy(golden):
    parsed = parse_bundle(
        _bundle(golden, ["weight_below_threshold"]), KernelConfig()
    )
    assert len(parsed) == 1


def test_parse_missing_items_list():
    with pytest.raises(LightClientError) as ei:
        parse_bundle({}, KernelConfig())
    assert ei.value.category is Category.INPUT


def test_parse_empty_bundle():
    with pytest.raises(LightClientError) as ei:
        parse_bundle({"items": []}, KernelConfig())
    assert ei.value.category is Category.INPUT


def test_parse_oversized_bundle(golden):
    cfg = KernelConfig(max_batch_size=1)
    with pytest.raises(LightClientError) as ei:
        parse_bundle(
            _bundle(
                golden,
                ["weight_below_threshold", "stale_round"],
            ),
            cfg,
        )
    assert ei.value.code is Code.BATCH_TOO_LARGE
    assert ei.value.category is Category.RESOURCE


def test_parse_bad_hex_indexed(golden):
    bundle = _bundle(golden, ["weight_below_threshold"])
    bundle["items"][0]["header"]["body_root"] = "not-hex"
    with pytest.raises(LightClientError) as ei:
        parse_bundle(bundle, KernelConfig())
    assert ei.value.details.get("index") == 0 or "body_root" in ei.value.details.get(
        "field", ""
    )


def test_load_invalid_json_file(tmp_path, golden):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(LightClientError) as ei:
        load_bundle_file(str(p), KernelConfig())
    assert ei.value.category is Category.INPUT


def test_replay_batch_contiguous_then_query(golden, kernel):
    from lc.types import Checkpoint

    kernel.install_checkpoint(
        Checkpoint.from_dict(golden["checkpoint"], committee_max_size=256)
    )
    chain = next(v for v in golden["vectors"] if v["kind"] == "chain")
    bundle = {
        "items": [
            {
                "header": it["header"],
                "certificate": it["certificate"],
                "next_committee": None,
            }
            for it in chain["items"]
        ]
    }
    parsed = parse_bundle(bundle, kernel.config)
    rep = kernel.apply_batch(parsed)
    assert rep.accepted is True
    assert rep.tip_after["tip_round"] == 103
