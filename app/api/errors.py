"""Typed API errors and the shared JSON error envelope."""
from __future__ import annotations

from typing import Any, Optional


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str,
                 details: Optional[Any] = None, request_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id


def error_envelope(code: str, message: str, request_id: str,
                   details: Any = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return body
