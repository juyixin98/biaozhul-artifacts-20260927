"""诊断记录：携带请求标识、关键状态与脱敏信息。

约定：
- 每条诊断带 code（对应 FailureCategory 或信息码）、severity、message、detail；
- URI 一律经 redact_uri 脱敏（去掉 query/fragment，常见 token 落点）后再进入诊断；
- DiagnosticLog 绑定 request_id，API 层每次请求一个实例。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def redact_uri(uri: Optional[str]) -> Optional[str]:
    """去掉 query 与 fragment，避免泄露签名 token 等敏感参数。"""
    if uri is None:
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        return "<unparseable-uri>"
    if not parts.query and not parts.fragment:
        return uri
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


@dataclass
class Diagnostic:
    code: str
    severity: str  # INFO | WARNING | ERROR
    message: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "detail": self.detail,
        }


class DiagnosticLog:
    """一次请求/一次作业的诊断集合。"""

    def __init__(self, request_id: Optional[str] = None):
        self.request_id = request_id or new_request_id()
        self.records: list[Diagnostic] = []

    def add(self, code: str, severity: str, message: str, **detail: Any) -> Diagnostic:
        # detail 中任何 uri 字段统一脱敏
        safe = {
            k: (redact_uri(v) if isinstance(v, str) and ("uri" in k or "url" in k) else v)
            for k, v in detail.items()
        }
        rec = Diagnostic(code=code, severity=severity, message=message, detail=safe)
        self.records.append(rec)
        return rec

    def info(self, code: str, message: str, **detail: Any) -> Diagnostic:
        return self.add(code, "INFO", message, **detail)

    def warning(self, code: str, message: str, **detail: Any) -> Diagnostic:
        return self.add(code, "WARNING", message, **detail)

    def error(self, code: str, message: str, **detail: Any) -> Diagnostic:
        return self.add(code, "ERROR", message, **detail)

    @property
    def has_errors(self) -> bool:
        return any(r.severity == "ERROR" for r in self.records)

    def to_list(self) -> list:
        return [r.to_dict() for r in self.records]
