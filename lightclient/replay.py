"""Offline replay engine.

Replays an ordered stream of headers against the kernel. Semantics:

  * strict order — headers must connect one-by-one to the trusted tip;
  * stop at the first rejection — no later item is attempted;
  * the rejected header (and everything after it) changes no state;
  * a ``preflight`` mode validates the whole batch's shape/bounds and
    resource limits *before* applying anything (used to distinguish
    RESOURCE_LIMIT from protocol conflicts).

The trust-period rule is evaluated on header timestamps (see kernel), so a
long offline gap surfaces deterministically as NEED_CHECKPOINT regardless
of wall-clock replay speed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import codec
from .config import LightClientConfig
from .errors import LightClientError
from .kernel import REJECTED, ApplyResult, LightClientKernel
from .types import Certificate, Header

STOPPED = "stopped"
APPLIED_ALL = "applied_all"
PREFLIGHT_FAILED = "preflight_failed"
NOT_RUN = "not_run"


@dataclass(frozen=True)
class ReplayItem:
    header: Header
    certificate: Certificate
    #: provenance label carried into the audit log (e.g. file offset)
    source: str = ""


@dataclass
class ReplayStep:
    index: int
    source: str
    digest_hex: str
    height: int
    decision: str
    error_code: str | None = None
    error_category: str | None = None
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayReport:
    run_id: str | None
    status: str
    applied: int
    total: int
    steps: list[ReplayStep] = field(default_factory=list)
    tip_after: dict[str, Any] | None = None
    failure_index: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == APPLIED_ALL

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "ok": self.ok,
            "applied": self.applied,
            "total": self.total,
            "failure_index": self.failure_index,
            "tip_after": self.tip_after,
            "steps": [
                {
                    "index": s.index,
                    "source": s.source,
                    "digest": s.digest_hex,
                    "height": s.height,
                    "decision": s.decision,
                    "error_code": s.error_code,
                    "error_category": s.error_category,
                    "reason": s.reason,
                    "detail": s.detail,
                }
                for s in self.steps
            ],
        }


def item_from_wire(
    header_wire: bytes, cert_wire: bytes, *, source: str = ""
) -> ReplayItem:
    header = codec.decode_header(header_wire)
    cert = codec.decode_certificate(cert_wire)
    if cert.header_digest != codec.header_digest(header):
        from .errors import SignatureInvalid

        raise SignatureInvalid("item certificate bound to different header")
    return ReplayItem(header=header, certificate=cert, source=source)


class ReplayEngine:
    def __init__(self, kernel: LightClientKernel) -> None:
        self.kernel = kernel

    def preflight(self, items: list[ReplayItem]) -> None:
        """Validate only bounds/shape. Does not touch kernel state."""
        cfg: LightClientConfig = self.kernel.config
        if len(items) > cfg.max_replay_batch:
            from .errors import ResourceLimit

            raise ResourceLimit(
                "replay batch exceeds configured maximum",
                {"batch_size": len(items), "limit": cfg.max_replay_batch},
            )
        # Decode every structure with bounds; also pin the certificate
        # binding before any state change is attempted.
        for i, item in enumerate(items):
            wire = codec.encode_header(item.header)
            if len(wire) > cfg.max_header_bytes:
                from .errors import ResourceLimit

                raise ResourceLimit(
                    f"item {i} header exceeds size bound",
                    {"index": i, "size": len(wire), "limit": cfg.max_header_bytes},
                )
            if len(item.certificate.votes) > cfg.max_certificate_votes:
                from .errors import ResourceLimit

                raise ResourceLimit(
                    f"item {i} certificate exceeds vote bound",
                    {
                        "index": i,
                        "votes": len(item.certificate.votes),
                        "limit": cfg.max_certificate_votes,
                    },
                )
            if item.certificate.header_digest != codec.header_digest(item.header):
                from .errors import SignatureInvalid

                raise SignatureInvalid(
                    f"item {i} certificate bound to different header",
                    {"index": i},
                )

    def replay(
        self,
        items: list[ReplayItem],
        *,
        run_id: str | None = None,
        preflight: bool = True,
    ) -> ReplayReport:
        report = ReplayReport(
            run_id=run_id or self.kernel.run_id,
            status=NOT_RUN,
            applied=0,
            total=len(items),
        )

        if preflight:
            try:
                self.preflight(items)
            except LightClientError as exc:
                step = ReplayStep(
                    index=-1,
                    source="preflight",
                    digest_hex="",
                    height=-1,
                    decision="rejected",
                    error_code=exc.code.value,
                    error_category=exc.category.value,
                    reason=exc.reason,
                    detail=exc.detail,
                )
                report.steps.append(step)
                report.status = PREFLIGHT_FAILED
                report.failure_index = -1
                report.tip_after = self.kernel.tip().to_dict()
                return report

        for i, item in enumerate(items):
            digest_hex = codec.hex_(codec.header_digest(item.header))
            try:
                result: ApplyResult = self.kernel.apply_header(
                    item.header, item.certificate, run_id=run_id
                )
            except LightClientError as exc:
                report.steps.append(
                    ReplayStep(
                        index=i,
                        source=item.source,
                        digest_hex=digest_hex,
                        height=item.header.height,
                        decision=REJECTED,
                        error_code=exc.code.value,
                        error_category=exc.category.value,
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                )
                report.status = STOPPED
                report.failure_index = i
                report.tip_after = self.kernel.tip().to_dict()
                return report

            report.steps.append(
                ReplayStep(
                    index=i,
                    source=item.source,
                    digest_hex=digest_hex,
                    height=item.header.height,
                    decision=result.decision,
                    detail=result.certificate.to_detail(),
                )
            )
            report.applied += 1

        report.status = APPLIED_ALL
        report.tip_after = self.kernel.tip().to_dict()
        return report
