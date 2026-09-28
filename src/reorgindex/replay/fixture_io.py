"""Shared fixture-package writing logic for the generator scripts."""
from __future__ import annotations

import json
from pathlib import Path

from ..crypto.keys import public_key_bytes
from .builder import BranchBuilder, FixtureKeys


def write_recording(
    out_dir: Path,
    *,
    keys: FixtureKeys,
    main: BranchBuilder,
    branches: dict[str, BranchBuilder],
    arrival_order: list[str],
    expected: dict,
    scenario: str,
    difficulty: int,
    finality_depth: int,
    crash_after: str | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Merge every block seen across branches (shared dict already).
    names = list(main.blocks.keys())
    for branch in branches.values():
        for name in branch.blocks:
            if name not in names:
                names.append(name)

    blocks_doc = {
        "scenario": scenario,
        "difficulty": difficulty,
        "finality_depth": finality_depth,
        "producer_pubkey": public_key_bytes(keys.producer).hex(),
        "producer_address": keys.addresses["producer"],
        "addresses": {k: v for k, v in keys.addresses.items()},
        "arrival_order": arrival_order,
        "crash_after": crash_after,
        "blocks": [
            {"name": name, "block": main.blocks[name]}
            for name in names
        ],
        "expected": expected,
    }
    (out_dir / "recording.json").write_text(
        json.dumps(blocks_doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    expected_doc = {
        "scenario": scenario,
        "difficulty": difficulty,
        "finality_depth": finality_depth,
        **expected,
    }
    (out_dir / "expected.json").write_text(
        json.dumps(expected_doc, indent=2, ensure_ascii=False), encoding="utf-8"
    )
