"""Job record model and status vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"        # pipeline produced a report (possibly "insufficient_evidence")
    FAILED = "failed"    # pipeline raised a classifiable error


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    request_id: str
    status: JobStatus
    created_at: str
    updated_at: str
    params: dict[str, Any]
    result: dict[str, Any] | None
    error_class: str | None
    error_message: str | None
    pipeline_version: str
