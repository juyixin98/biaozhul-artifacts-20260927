"""Local end-to-end demo without a running server (in-process engine).

Run:  .venv/bin/python examples/demo.py

It shows: same value under different local codes merging, NULL handling,
duplicate canonicalization, a strict-width overflow failure, and the
independent round-trip verification. Everything is synthetic and local.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings
from app.core.errors import DicunifyError
from app.service.engine import UnifyEngine
from app.store.sqlite_store import JobStore


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="dicunify-demo-"))
    settings = Settings(db_path=tmp / "demo.db",
                        max_cardinality=2**32 - 1, log_level="WARNING")
    engine = UnifyEngine(settings, JobStore(settings.db_path))

    print("== 1) overlapping local codes ==")
    resp = engine.run_json({
        "value_type": "string",
        "client_run_id": "demo-1",
        "batches": [
            {"batch_id": "b0", "dictionary": ["a", "b"],
             "indices": [0, 1, 0], "validity": [True, False, True]},
            {"batch_id": "b1", "dictionary": ["b", "c", "a"],
             "indices": [0, 1, 2]},
        ],
    })
    print(json.dumps(resp.body, indent=2, ensure_ascii=False))

    print("\n== 2) duplicate local dictionary entries (opt-in dedupe) ==")
    resp = engine.run_json({
        "value_type": "string",
        "dedupe_local_dictionary": True,
        "batches": [
            {"batch_id": "dup", "dictionary": ["x", "x", "y", "x"],
             "indices": [0, 1, 2, 3]},
        ],
    })
    print("global:", resp.body["global_dictionary"])
    print("remap :", resp.body["batch_remaps"][0]["global_indices"])

    print("\n== 3) strict uint8 overflow (300 distinct values) ==")
    try:
        engine.run_json({
            "value_type": "string",
            "index_policy": "strict", "target_width": 8,
            "batches": [{"batch_id": "big",
                         "dictionary": [f"v{i}" for i in range(300)],
                         "indices": list(range(300))}],
        })
    except DicunifyError as exc:
        print(f"classified failure: {exc.code} -> {exc.details}")

    print("\nDemo SQLite DB:", settings.db_path)


if __name__ == "__main__":
    main()
