"""End-to-end service call example (no external services).

Runs the API in-process with :class:`fastapi.testclient.TestClient` against a
throwaway database, feeds the short-fork fixture, and prints every notable
response: pending orphan, switch with rollback range, duplicate-tx query and
diagnostics.  Equivalent curl commands are in docs/REPRODUCE.md.

Run::

    . .venv/bin/activate && python scripts/call_service_example.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from reorgindex.api.main import create_app  # noqa: E402
from reorgindex.config import Settings  # noqa: E402


def main() -> None:
    recording = json.loads((ROOT / "fixtures" / "short_fork" / "recording.json").read_text())
    settings = Settings(
        database_path=ROOT / "run" / "example.db",
        finality_depth=3,
        allowed_difficulties=frozenset({4, 16}),
        service_name="example",
        log_level="ERROR",
    )
    by_name = {b["name"]: b["block"] for b in recording["blocks"]}

    with TestClient(create_app(settings)) as client:
        print("== POST /blocks in arrival order ==")
        for name in recording["arrival_order"]:
            resp = client.post(
                "/blocks", json=by_name[name], headers={"X-Request-ID": f"example-{name}"}
            )
            result = resp.json().get("result", resp.json())
            print(f"{name:>3} -> HTTP {resp.status_code} {result.get('outcome')}", end="")
            switch = (result.get("switch") or {})
            if switch.get("rollback_height_range"):
                print(f"  rollback={switch['rollback_height_range']}", end="")
            if result.get("released"):
                print(f"  released={len(result['released'])} block(s)", end="")
            print()

        print("\n== GET /chain ==")
        print(json.dumps(client.get("/chain").json(), indent=2))

        dup = recording["expected"]["duplicate_txid"]
        print("\n== GET /transactions/{duplicate txid} (must contribute exactly once) ==")
        tx = client.get(f"/transactions/{dup}").json()
        print(json.dumps(tx, indent=2))
        assert len([o for o in tx["occurrences"] if o["on_active"]]) == 1

        print("\n== GET /diagnostics?limit=5 ==")
        diag = client.get("/diagnostics?limit=5").json()["diagnostics"]
        for row in diag:
            print(
                f"seq={row['seq']} req={row['request_id']} outcome={row['outcome']} "
                f"reason={row['reason']} height={row['height']} tip_height={row['active_height']}"
            )


if __name__ == "__main__":
    main()
