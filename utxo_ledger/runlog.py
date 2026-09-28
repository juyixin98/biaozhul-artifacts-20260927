"""Structured JSONL run logs for replay/verification runs.

Every run gets a stable ``run_id`` (UTC timestamp + 6 random hex chars, e.g.
``run-20260928T091530Z-3f9a1c``) and one JSON object per line under the log
directory: ``<logdir>/<run_id>.jsonl`` plus a final summary
``<logdir>/<run_id>.summary.json``.

Events capture enough intermediate state to reproduce a verdict manually:
run start (environment/versions), each block attempt (height, txids, sizes),
each per-transaction check (stage, code, category, sums), commit vs. rejection,
and the UTXO snapshot hash/count before and after. Nothing here is part of the
consensus path -- it is observability only.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import sys
from datetime import datetime, timezone
from typing import Any, Mapping


def utc_run_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{ts}-{secrets.token_hex(3)}"


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def utxo_set_fingerprint(snapshot: Mapping[tuple[str, int], dict]) -> dict:
    """Stable digest of a UTXO snapshot; identical state => identical digest."""
    canonical = canonical_json_bytes(
        [
            {"txid": txid, "vout": vout, **entry}
            for (txid, vout), entry in sorted(snapshot.items())
        ]
    )
    return {
        "count": len(snapshot),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


class RunLogger:
    def __init__(self, logdir: str, run_id: str | None = None) -> None:
        self.run_id = run_id or utc_run_id()
        self.logdir = logdir
        os.makedirs(logdir, exist_ok=True)
        self.path = os.path.join(logdir, f"{self.run_id}.jsonl")
        self._fh = open(self.path, "w", encoding="utf-8")
        self.events = 0

    def event(self, name: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "event": name,
            **fields,
        }
        self._fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        self._fh.flush()
        self.events += 1

    # -- domain convenience methods ------------------------------------------
    def run_start(self, component: str, **extra: Any) -> None:
        self.event(
            "run_start",
            component=component,
            python=sys.version.split()[0],
            platform=platform.platform(),
            **extra,
        )

    def block_start(
        self,
        seq: int,
        height: int | None,
        raw_size: int,
        txids: list[str],
        prev_hash: str | None = None,
    ) -> None:
        self.event(
            "block_attempt",
            seq=seq,
            height=height,
            prev_hash=prev_hash,
            raw_size=raw_size,
            tx_count=len(txids),
            txids=txids,
        )

    def tx_check(
        self,
        seq: int,
        tx_index: int,
        stage: str,
        verdict: str,
        *,
        txid: str | None = None,
        code: str | None = None,
        category: str | None = None,
        reason: str | None = None,
        **state: Any,
    ) -> None:
        self.event(
            "tx_check",
            seq=seq,
            tx_index=tx_index,
            stage=stage,
            verdict=verdict,
            txid=txid,
            code=code,
            category=category,
            reason=reason,
            **state,
        )

    def block_result(
        self,
        seq: int,
        verdict: str,
        *,
        height: int | None = None,
        block_hash: str | None = None,
        code: str | None = None,
        category: str | None = None,
        reason: str | None = None,
        fee_total: int | None = None,
        snapshot_before: dict | None = None,
        snapshot_after: dict | None = None,
        **extra: Any,
    ) -> None:
        self.event(
            "block_result",
            seq=seq,
            verdict=verdict,
            height=height,
            block_hash=block_hash,
            code=code,
            category=category,
            reason=reason,
            fee_total=fee_total,
            snapshot_before=snapshot_before,
            snapshot_after=snapshot_after,
            **extra,
        )

    def close(self, summary: Mapping[str, Any]) -> str:
        self.event("run_end", **summary)
        self._fh.close()
        summary_path = os.path.join(self.logdir, f"{self.run_id}.summary.json")
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(
                {"run_id": self.run_id, "events": self.events, **summary},
                fh,
                sort_keys=True,
                indent=2,
            )
        return summary_path

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, *exc) -> None:
        if not self._fh.closed:
            self._fh.close()
