"""Per-PID continuity counter checking (ISO/IEC 13818-1 §2.4.3.2).

Rules implemented:

* The 4-bit counter increments by 1 with each successive packet that
  **carries a payload** on a PID. Packets without payload (adaptation
  field only, ``adaptation_field_control == 2``) do not increment it:
  they repeat the previous counter value.
* An adaptation-only packet that nevertheless increments the counter is
  tolerated (many real encoders do this) and reported at INFO, not as a
  loss.
* A repeated counter on a payload packet whose payload bytes are
  identical to the previous payload packet is a **duplicate** packet.
* A repeated counter with different payload bytes is a stall / illegal
  counter reuse, which is a real discontinuity.
* A jump in the counter means ``missing`` packets were lost.
* The adaptation field ``discontinuity_indicator`` is the sender's own
  declaration: the counter is accepted as-is and the event is recorded
  distinctly from a real loss. It never increments the loss counter.
* After a byte-stream sync loss, per-PID baselines are re-established on
  the next observed packet instead of fabricating losses for a gap that
  might simply be unparsable garbage.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from ..diagnostics import Code, DiagnosticsCollector
from .packets import PacketHeader


class CCVerdict(enum.Enum):
    FIRST = "first"                       # baseline packet for this PID
    OK = "ok"
    DUPLICATE = "duplicate"               # same CC, identical payload
    LOST = "lost"                         # CC jumped; packets missing
    STALL = "stall"                       # same CC on payload, payload differs
    AF_ONLY_REPEAT = "af_only_repeat"     # adaptation-only reusing CC (legal)
    AF_ONLY_INCREMENT = "af_only_increment"  # adaptation-only incremented (tolerated)
    DISCONTINUITY_DECLARED = "discontinuity_declared"
    RESET_AFTER_SYNC = "reset_after_sync"


@dataclass
class CCResult:
    verdict: CCVerdict
    missing: int = 0


@dataclass
class PidCCState:
    last_cc: Optional[int] = None
    last_payload_cc: Optional[int] = None
    last_payload: bytes = b""
    packets: int = 0
    payload_packets: int = 0
    duplicates: int = 0
    lost: int = 0
    stalls: int = 0
    declared_discontinuities: int = 0
    need_baseline: bool = True  # set again after stream sync loss


class ContinuityTracker:
    def __init__(self, diagnostics: DiagnosticsCollector):
        self._diag = diagnostics
        self._state: dict[int, PidCCState] = {}

    def state_for(self, pid: int) -> PidCCState:
        return self._state.setdefault(pid, PidCCState())

    def snapshot(self) -> dict[int, PidCCState]:
        return dict(self._state)

    def reset_after_sync_loss(self, offset: Optional[int]) -> None:
        """Forget per-PID counter baselines after a byte-stream sync break."""
        for state in self._state.values():
            state.need_baseline = True
        self._diag.info(
            Code.CC_RESET_AFTER_SYNC,
            "per-PID continuity baselines re-established after stream sync loss",
            offset=offset,
            pids=list(self._state.keys()),
        )

    def check(
        self,
        header: PacketHeader,
        payload: bytes,
    ) -> CCResult:
        pid = header.pid
        cc = header.continuity_counter
        state = self.state_for(pid)
        state.packets += 1

        if state.need_baseline or state.last_payload_cc is None:
            state.last_cc = cc
            state.last_payload_cc = cc if header.has_payload else None
            state.last_payload = bytes(payload) if header.has_payload else state.last_payload
            state.need_baseline = False
            if header.has_payload:
                state.payload_packets += 1
            return CCResult(CCVerdict.FIRST)

        declared = (
            header.adaptation_field is not None
            and header.adaptation_field.discontinuity_indicator
        )
        if declared:
            state.declared_discontinuities += 1
            self._diag.info(
                Code.CC_DISCONTINUITY_FLAG,
                "sender-declared discontinuity; counter accepted without loss accounting",
                pid=pid,
                offset=header.offset,
                continuity_counter=cc,
                last_counter=state.last_cc,
            )
            self._commit(state, header, cc, payload)
            return CCResult(CCVerdict.DISCONTINUITY_DECLARED)

        if not header.has_payload:
            # Adaptation-only: counter shall equal the last payload CC.
            if cc == state.last_payload_cc:
                return CCResult(CCVerdict.AF_ONLY_REPEAT)
            if cc == (state.last_payload_cc + 1) & 0xF:
                self._diag.info(
                    Code.CC_AF_ONLY_INCREMENT,
                    "adaptation-only packet incremented counter (tolerated per common practice)",
                    pid=pid,
                    offset=header.offset,
                    continuity_counter=cc,
                    expected=state.last_payload_cc,
                )
                # Real encoders that increment on adaptation-only packets
                # keep a single running counter; adopt the new value so the
                # following payload packet is compared against it.
                state.last_cc = cc
                state.last_payload_cc = cc
                return CCResult(CCVerdict.AF_ONLY_INCREMENT)
            missing = (cc - ((state.last_payload_cc + 1) & 0xF)) & 0xF
            state.lost += max(1, missing)
            self._diag.error(
                Code.CC_LOST,
                "unexpected counter on adaptation-only packet",
                pid=pid,
                offset=header.offset,
                continuity_counter=cc,
                expected=state.last_payload_cc,
                missing=max(1, missing),
            )
            state.last_cc = cc
            return CCResult(CCVerdict.LOST, max(1, missing))

        # Current packet carries a payload.
        state.payload_packets += 1
        expected = (state.last_payload_cc + 1) & 0xF

        if cc == expected:
            self._commit(state, header, cc, payload)
            return CCResult(CCVerdict.OK)

        if cc == state.last_payload_cc:
            if payload and bytes(payload) == state.last_payload:
                state.duplicates += 1
                self._diag.warning(
                    Code.CC_DUPLICATE,
                    "duplicate packet: counter repeated with identical payload",
                    pid=pid,
                    offset=header.offset,
                    continuity_counter=cc,
                    payload_bytes=payload,  # redacted by collector (length only)
                )
                return CCResult(CCVerdict.DUPLICATE)

            state.stalls += 1
            self._diag.error(
                Code.CC_STALL,
                "counter repeated with different payload bytes (illegal reuse)",
                pid=pid,
                offset=header.offset,
                continuity_counter=cc,
                previous_payload_bytes=state.last_payload,
                payload_bytes=payload,
            )
            return CCResult(CCVerdict.STALL)

        missing = (cc - expected) & 0xF
        state.lost += missing
        self._diag.error(
            Code.CC_LOST,
            "continuity counter gap indicates lost packets",
            pid=pid,
            offset=header.offset,
            continuity_counter=cc,
            expected=expected,
            missing=missing,
        )
        self._commit(state, header, cc, payload)
        return CCResult(CCVerdict.LOST, missing)

    @staticmethod
    def _commit(
        state: PidCCState, header: PacketHeader, cc: int, payload: bytes
    ) -> None:
        state.last_cc = cc
        if header.has_payload:
            state.last_payload_cc = cc
            state.last_payload = bytes(payload)
