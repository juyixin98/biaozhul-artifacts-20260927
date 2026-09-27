"""API 层业务异常：由 main 中的异常处理器转成统一错误信封。"""
from __future__ import annotations

from ..diagnostics.errors import HTTP_STATUS


class ServiceError(Exception):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        http_status: int | None = None,
        position: int | None = None,
        expression: str | None = None,
        version: int | None = None,
        uncertainty: list[str] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.http_status = http_status or HTTP_STATUS.get(category, 500)
        self.position = position
        self.expression = expression
        self.version = version
        self.uncertainty = uncertainty or []
        self.request_id = request_id

    @classmethod
    def from_outcome(cls, outcome, *, expression: str | None = None) -> "ServiceError":
        return cls(
            category=outcome.error_category,
            message=outcome.error_message,
            position=outcome.error_position,
            expression=expression if expression is not None else outcome.expression,
            version=outcome.version,
            uncertainty=outcome.uncertainty,
            request_id=outcome.request_id,
        )
