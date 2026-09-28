"""审计与诊断接口。

设计约束（来自需求）：
- 日志只包含**份额指纹**，绝不出现秘密、y 明文、标签明文；
- 每条记录带请求/记录标识与关键状态，说明"为什么接受、拒绝或无法判定"；
- 结构化 JSONL 落盘，同时经 /audit 查询；
- 任何写入路径都先过白名单，防止把敏感 kwargs 误打进日志。
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# 允许出现在审计记录 detail 中的键。其余键一律丢弃（纵深防泄漏）。
_ALLOWED_DETAIL_KEYS = {
    "set_id", "threshold", "field", "distinct_x_count", "submitted_count",
    "distinct_accepted_fingerprints", "duplicate_x", "unknown_sets", "field_mismatches",
    "threshold_mismatches", "bad_tag_fingerprints", "bad_length_fingerprints",
    "duplicate_conflict_fingerprints", "accepted_fingerprints",
    "expected_secret_len", "candidate_secret_fp", "secret_fp",
    "share_count", "reason", "committed",
}
# 即便误传，这些键名也强制不记录。
_FORBIDDEN_SUBSTRINGS = ("secret", "y_raw", "tag_raw", "key", "envelope")


def _redact_detail(detail: dict) -> dict:
    clean = {}
    for key, value in detail.items():
        low = key.lower()
        if key not in _ALLOWED_DETAIL_KEYS:
            continue
        if any(bad in low for bad in _FORBIDDEN_SUBSTRINGS):
            # secret_fp / candidate_secret_fp 是哈希指纹，属允许列表内的指纹，放行；
            # 其余含敏感子串的键丢弃。
            if not key.endswith("_fp"):
                continue
        clean[key] = _redact_value(value)
    return clean


def _redact_value(value):
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items() if _safe_key(k)}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)  # 其它类型字符串化，避免对象 repr 泄漏


def _safe_key(key: str) -> bool:
    low = str(key).lower()
    return not any(bad in low for bad in _FORBIDDEN_SUBSTRINGS) or low.endswith("_fp")


OUTCOME_ACCEPTED = "ACCEPTED"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_INDETERMINATE = "INDETERMINATE"


class AuditLogger:
    """线程安全的 JSONL 审计记录器 + 内存检索（测试/查询用）。"""

    def __init__(self, audit_path: str):
        self._lock = threading.Lock()
        self._records: list[dict] = []
        self._path = Path(audit_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file_logger = logging.getLogger("tss.audit")
        self._file_logger.setLevel(logging.INFO)
        if not any(
            isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", None) == str(self._path.resolve())
            for h in self._file_logger.handlers
        ):
            handler = logging.FileHandler(self._path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._file_logger.addHandler(handler)
        self._file_logger.propagate = False

    def record(
        self,
        *,
        request_id: str,
        stage: str,
        outcome: str,
        reason: str,
        detail: dict | None = None,
    ) -> dict:
        entry = {
            "record_id": f"rec_{uuid.uuid4().hex[:16]}",
            "request_id": request_id,
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "outcome": outcome,
            "reason": reason,
            "detail": _redact_detail(detail or {}),
        }
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._records.append(entry)
            self._file_logger.info(line)
        return entry

    def query(
        self, *, request_id: str | None = None, set_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        with self._lock:
            items = list(self._records)
        if request_id:
            items = [r for r in items if r["request_id"] == request_id]
        if set_id:
            items = [r for r in items if r["detail"].get("set_id") == set_id]
        return items[-limit:]


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"
