"""Application service: orchestrates policy -> kernel -> audit -> log."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core.kernel import Kernel
from .core.policy import Policy
from .logging_setup import log_review
from .state.audit import AuditStore
from .state.fixture import ReadOnlyFixture


@dataclass
class ReviewResponse:
    request_id: str
    result: dict[str, Any]


class ReviewService:
    def __init__(self, policy: Policy, fixture: ReadOnlyFixture | None,
                 audit: AuditStore, logger):
        self.policy = policy
        self.fixture = fixture
        self.audit = audit
        self.logger = logger

    def review(self, *, template: str, params: dict[str, Any] | None = None,
               slots: dict[str, str] | None = None,
               inline_policy: dict[str, Any] | None = None,
               request_id: str | None = None) -> ReviewResponse:
        request_id = request_id or self.audit.new_request_id()
        effective = self.policy.with_overrides(inline_policy)
        kernel = Kernel(effective, self.fixture)
        result = kernel.review(template, params=params, slots=slots)
        result_dict = result.to_dict()

        self.audit.append(
            request_id=request_id, template=template, result_dict=result_dict)
        log_review(
            self.logger, request_id=request_id,
            verdict=result_dict["verdict"], codes=result_dict["codes"],
            stmt=result_dict["statement_type"], params=params)
        return ReviewResponse(request_id=request_id, result=result_dict)

    def fetch_audit(self, request_id: str) -> dict[str, Any] | None:
        return self.audit.get(request_id)

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.audit.list(limit=limit)

    def chain_report(self) -> dict[str, Any]:
        report = self.audit.verify_chain()
        return {"ok": report.ok, "records": report.records,
                "first_bad_seq": report.first_bad_seq, "reason": report.reason}
