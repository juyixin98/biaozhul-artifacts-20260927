"""Static configuration for the light client.

All bounds live here so "resource exhaustion" is a defined, testable
condition rather than an arbitrary OS limit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LightClientConfig:
    #: Identifier of the fixed local test chain; headers/checkpoints are
    #: rejected unless their chain_id matches.
    chain_id: str = "local-test-chain-0001"

    #: Maximum forward time gap (seconds) between the trusted tip and a new
    #: header. A header strictly beyond the tip timestamp + this value is
    #: rejected with NEED_CHECKPOINT (the trust period has lapsed).
    trust_period_seconds: int = 3600

    #: Exact quorum weight (>= this many weight units) required to authorize
    #: a header or committee change.
    quorum_weight: int = 2

    #: Hard bounds used to classify RESOURCE_LIMIT.
    max_committee_members: int = 64
    max_certificate_votes: int = 64
    max_header_bytes: int = 64 * 1024
    max_replay_batch: int = 100
    max_request_bytes: int = 1024 * 1024

    @classmethod
    def from_env(cls) -> "LightClientConfig":
        def _int(name: str, default: int) -> int:
            raw = os.environ.get(name)
            return int(raw) if raw is not None else default

        return cls(
            chain_id=os.environ.get("LC_CHAIN_ID", cls.chain_id),
            trust_period_seconds=_int("LC_TRUST_PERIOD", cls.trust_period_seconds),
            quorum_weight=_int("LC_QUORUM_WEIGHT", cls.quorum_weight),
        )
