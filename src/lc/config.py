"""Kernel configuration: trust period and resource limits."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KernelConfig:
    # A tip older than ``now - trust_period_ms`` is beyond the trust period;
    # fresh committee data (a new trusted checkpoint) is then required.
    trust_period_ms: int = 7 * 24 * 60 * 60 * 1000  # 7 days
    # How far in the future an inbound header may be (small clock skew slack).
    future_skew_ms: int = 30 * 1000  # 30 seconds
    # Resource limits (-> RESOURCE category).
    max_batch_size: int = 256
    committee_max_size: int = 256

    def __post_init__(self) -> None:
        if self.trust_period_ms <= 0:
            raise ValueError("trust_period_ms must be positive")
        if self.future_skew_ms < 0:
            raise ValueError("future_skew_ms must be non-negative")
        if self.max_batch_size <= 0:
            raise ValueError("max_batch_size must be positive")
        if self.committee_max_size <= 0:
            raise ValueError("committee_max_size must be positive")
