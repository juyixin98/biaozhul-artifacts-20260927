"""审计接口：只追加 JSONL + HMAC 哈希链（可检测篡改/断链）。

每条记录结构::

    {"seq": n, "ts": ..., "run_id": ..., "action": ...,
     "status": succeeded|failed|error, "actor": ..., "detail": {...},
     "prev_hash": "<hex>", "record_hash": "<hex>"}

``record_hash = HMAC(key, seq|ts|run_id|action|status|prev_hash|body)``。

状态区分（验收要求）：
* ``succeeded`` —— 业务成功；
* ``failed``    —— 明确的业务失败（如 K_UNREACHABLE、校验拒绝）；
* ``error``     —— 未预期异常。

失败绝不记为 succeeded；另提供 :func:`verify_chain` 离线核验链完整性。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _canonical_body(record: dict[str, Any]) -> bytes:
    body = {k: v for k, v in record.items() if k not in ("record_hash",)}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def compute_record_hash(key: bytes, record: dict[str, Any]) -> str:
    payload = (
        str(record["seq"]).encode()
        + b"|"
        + record["ts"].encode()
        + b"|"
        + str(record.get("run_id", "")).encode()
        + b"|"
        + record["action"].encode()
        + b"|"
        + record["status"].encode()
        + b"|"
        + record["prev_hash"].encode()
        + b"|"
        + _canonical_body(record)
    )
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


class AuditLog:
    """线程安全的只追加审计日志。"""

    def __init__(self, path: str | Path, signing_key: bytes) -> None:
        self.path = Path(path)
        self.key = signing_key
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def _last_hash(self) -> tuple[str, int]:
        last = GENESIS
        seq = 0
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                last = rec["record_hash"]
                seq = rec["seq"]
        return last, seq

    def append(
        self,
        action: str,
        status: str,
        *,
        run_id: str = "",
        actor: str = "local-synthetic",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in ("succeeded", "failed", "error"):
            raise ValueError(f"unknown audit status {status!r}")
        with self._lock:
            prev_hash, seq = self._last_hash()
            record: dict[str, Any] = {
                "seq": seq + 1,
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": run_id,
                "action": action,
                "status": status,
                "actor": actor,
                "detail": detail or {},
                "prev_hash": prev_hash,
            }
            record["record_hash"] = compute_record_hash(self.key, record)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            return record

    def read_all(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def verify_chain(self) -> dict[str, Any]:
        return verify_chain(self.path, self.key)


def verify_chain(path: str | Path, key: bytes) -> dict[str, Any]:
    """核验审计链：序号连续、prev_hash 衔接、HMAC 全部有效。"""
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    p = Path(path)
    if not p.exists():
        return {"ok": True, "records": 0, "errors": []}
    with p.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                errors.append({"line": lineno, "error": f"invalid json: {exc}"})

    prev = GENESIS
    for i, rec in enumerate(records):
        seq_expected = i + 1
        if rec.get("seq") != seq_expected:
            errors.append({"line": seq_expected, "error": f"seq gap: got {rec.get('seq')}"})
        if rec.get("prev_hash") != prev:
            errors.append(
                {"line": rec.get("seq"), "error": "prev_hash mismatch (chain broken)"}
            )
        expected = compute_record_hash(key, rec)
        if not hmac.compare_digest(expected, rec.get("record_hash", "")):
            errors.append({"line": rec.get("seq"), "error": "HMAC mismatch (tampered)"})
        prev = rec.get("record_hash", "")

    return {
        "ok": not errors,
        "records": len(records),
        "errors": errors,
        "last_hash": prev if records else GENESIS,
    }
