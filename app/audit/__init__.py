"""审计查询接口（应用服务层，不直接依赖 FastAPI）。

把存储行转成对外可解释结构：关联 request_id、规则档版本、引擎版本、
关键步骤、位置映射；失败原因与不确定结论单列；原文解密需显式授权。
"""
from __future__ import annotations

from typing import Any

from .. import __version__
from ..core.redactor import MappingRecord
from ..state.audit_store import AuditStore, StoredMapping, StoredRequest


class AuditDenied(Exception):
    """审计访问被拒绝；reason 为机器可读分类码。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AuditService:
    def __init__(self, store: AuditStore, audit_token: str) -> None:
        self._store = store
        self._token = audit_token

    def _authorize(self, token: str | None) -> None:
        # 恒定时间比较，避免令牌猜测的时序侧信道
        import hmac
        if token is None or not hmac.compare_digest(token, self._token):
            raise AuditDenied("AUDIT_TOKEN_INVALID")

    # ------------------------------------------------------------------ #
    def request_summary(self, request_id: str) -> dict[str, Any]:
        """无需令牌：只返回可公开的处理元数据（不含输出与原文）。"""
        rec = self._store.get_request(request_id)
        if rec is None:
            raise AuditDenied("REQUEST_NOT_FOUND")
        return {
            "request_id": rec.request_id,
            "profile": rec.profile_name,
            "profile_version": rec.profile_version,
            "engine_version": rec.engine_version,
            "mode": rec.mode,
            "status": rec.status,
            "error_code": rec.error_code,
            "error_message": rec.error_message,
            "original_length": rec.original_length,
            "output_length": rec.output_length,
            "chunks_received": rec.chunks_received,
            "created_at": rec.created_at,
            "finalized_at": rec.finalized_at,
        }

    def request_detail(self, request_id: str, token: str) -> dict[str, Any]:
        """需令牌：返回脱敏输出、映射（不含原文）、失败/不确定、步骤。"""
        self._authorize(token)
        rec = self._store.get_request(request_id)
        if rec is None:
            raise AuditDenied("REQUEST_NOT_FOUND")
        mappings = self._store.get_mappings(request_id, decrypt=False)
        return {
            **self.request_summary(request_id),
            "redacted_output": rec.redacted_output,
            "mappings": [_mapping_dict(m, include_original=False)
                         for m in mappings],
            "uncertainties": [
                {"code": u.code, "start": u.start, "end": u.end,
                 "detail": u.detail}
                for u in self._store.get_uncertainties(request_id)
            ],
            "events": self._store.get_events(request_id),
        }

    def mapping_original(self, request_id: str, index: int,
                         token: str) -> dict[str, Any]:
        """需令牌的单条原文审计：给出原文、位置、规则、完整性哈希。"""
        self._authorize(token)
        mappings = self._store.get_mappings(request_id, decrypt=True)
        if not mappings:
            raise AuditDenied("REQUEST_NOT_FOUND")
        if not 0 <= index < len(mappings):
            raise AuditDenied("MAPPING_INDEX_OUT_OF_RANGE")
        m = mappings[index]
        return {
            "request_id": request_id,
            "index": index,
            "rule_id": m.rule_id,
            "source": m.source,
            "key": m.key,
            "original": m.original_text,
            "original_sha256": m.original_sha256,
            "original_span": [m.original_start, m.original_end],
            "output_span": [m.output_start, m.output_end],
            "replacement": m.replacement,
        }

    def list_requests(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self.summary_of(r) for r in self._store.list_requests(limit)]

    @staticmethod
    def summary_of(rec: StoredRequest) -> dict[str, Any]:
        return {
            "request_id": rec.request_id,
            "profile": rec.profile_name,
            "profile_version": rec.profile_version,
            "engine_version": rec.engine_version,
            "status": rec.status,
            "error_code": rec.error_code,
            "original_length": rec.original_length,
            "output_length": rec.output_length,
        }

    @staticmethod
    def verify_offset_consistency(
        output: str, mappings: list[MappingRecord | StoredMapping]
    ) -> list[str]:
        """独立校验：输出在映射区间应等于 replacement，区间按序不重叠。"""
        problems: list[str] = []
        prev_end = 0
        for i, m in enumerate(mappings):
            if m.output_start < prev_end:
                problems.append(f"mapping[{i}] 输出区间重叠")
            seg = output[m.output_start:m.output_end]
            if seg != m.replacement:
                problems.append(
                    f"mapping[{i}] 输出区间内容={seg!r} 不等于 "
                    f"replacement={m.replacement!r}")
            prev_end = m.output_end
        return problems


def _mapping_dict(m: StoredMapping, *, include_original: bool) -> dict[str, Any]:
    return {
        "index": m.index,
        "rule_id": m.rule_id,
        "source": m.source,
        "key": m.key,
        "original_span": [m.original_start, m.original_end],
        "output_span": [m.output_start, m.output_end],
        "replacement": m.replacement,
        "original_sha256": m.original_sha256,
        **({"original": m.original_text} if include_original else {}),
    }
