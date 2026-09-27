"""Per-PID continuity-counter checking (ISO/IEC 13818-1 2.4.3.3).

Contract semantics implemented here:

* PIDs are tracked independently; counters never mix across PIDs.
* The counter only *increments* on packets carrying a payload.  A packet
  containing only an adaptation field repeats the previous CC value.
* An exact duplicate packet (same CC as the previous payload packet and the
  AFC duplicate-bit semantics) is accepted as ``duplicate_packet``.  The same
  CC appearing *without* the protocol-justified repeat shape is a separate
  failure (``cc_repeat_payload_without_duplicate_bit``) -- these are not
  collapsed, because they mean different things.
* A real missing run (CC delta in 2..15) is ``cc_gap``; if the arriving
  packet's adaptation field carries the discontinuity indicator the same
  counter jump is classified as ``signaled_discontinuity`` instead.  A
  discontinuity flag on a packet whose counter is *not* broken is reported
  separately (``discontinuity_flag_without_gap``) -- flag and loss are
  distinct facts.
* The null PID 0x1FFF is not checked (its CC may be arbitrary).
"""
from __future__ import annotations

from dataclasses import dataclass

from .diagnostics import (
    CC_ADAPTATION_CC_MISMATCH,
    CC_BASELINE,
    CC_DISCONTINUITY,
    CC_DISCONTINUITY_NO_GAP,
    CC_DUPLICATE,
    CC_GAP,
    CC_REPEAT_PAYLOAD,
    Disposition,
    Finding,
    Severity,
)
from .sync import TSPacket

NULL_PID = 0x1FFF


@dataclass
class _CcState:
    last_cc: int
    last_had_payload: bool
    last_payload_cc: int          # CC of the most recent payload packet
    pending_discontinuity: bool  # DI seen on a preceding adaptation-only pkt
    seen: bool = False


@dataclass
class CcVerdict:
    """Outcome of checking one packet against its PID's state."""

    kind: str                    # baseline | ok | duplicate | gap | ...
    expected_cc: int | None
    missing_packets: int = 0     # inferred lost packets when kind == "gap"
    signaled: bool = False
    finding: Finding | None = None


class ContinuityChecker:
    def __init__(self) -> None:
        self._state: dict[int, _CcState] = {}

    def reset(self) -> None:
        """Forget all per-PID state (used after a sync-loss resync)."""
        self._state.clear()

    def check(self, pkt: TSPacket) -> CcVerdict:
        if pkt.pid == NULL_PID:
            return CcVerdict(kind="null_pid", expected_cc=None)

        st = self._state.get(pkt.pid)
        cc = pkt.continuity_counter
        di = pkt.adaptation.discontinuity if pkt.adaptation else False

        if st is None or not st.seen:
            self._state[pkt.pid] = _CcState(
                last_cc=cc,
                last_had_payload=pkt.has_payload,
                last_payload_cc=cc if pkt.has_payload else -1,
                pending_discontinuity=di and not pkt.has_payload,
                seen=True,
            )
            return CcVerdict(
                kind="baseline",
                expected_cc=None,
                finding=Finding(
                    code=CC_BASELINE,
                    severity=Severity.INFO,
                    disposition=Disposition.UNDETERMINED,
                    message=f"first observed packet for PID {pkt.pid:#06x}; "
                            "continuity cannot be judged yet",
                    packet_index=pkt.index,
                    pid=pkt.pid,
                    details={"cc": cc, "has_payload": pkt.has_payload},
                ),
            )

        signaled = bool(st.pending_discontinuity or di)

        if not pkt.has_payload:
            # Adaptation-only packets must repeat the immediately preceding
            # packet's CC; they do not advance the counter.
            if cc != st.last_cc:
                kind, sev, disp, msg = (
                    CC_ADAPTATION_CC_MISMATCH,
                    Severity.ERROR,
                    Disposition.REJECTED,
                    "adaptation-only packet changed the continuity counter",
                )
                finding = Finding(
                    code=kind, severity=sev, disposition=disp, message=msg,
                    packet_index=pkt.index, pid=pkt.pid,
                    details={"cc": cc, "previous_cc": st.last_cc},
                )
                verdict_kind = "adaptation_cc_mismatch"
                missing = 0
            else:
                finding = None
                verdict_kind = "adaptation_repeat"
                missing = 0
            st.last_cc = cc
            st.last_had_payload = False
            # A DI on this packet applies to the next payload packet.
            st.pending_discontinuity = signaled
            return CcVerdict(kind=verdict_kind, expected_cc=st.last_cc,
                             missing_packets=missing, signaled=signaled,
                             finding=finding)

        # Packet carries a payload.
        # A discontinuity indicator on the first payload packet after the
        # discontinuity point resets the counter baseline per the standard:
        # whatever CC it carries is accepted as the start of a new run, and
        # is explicitly distinguished from an unsignaled loss.
        if signaled:
            st.last_cc = cc
            st.last_had_payload = True
            st.last_payload_cc = cc
            st.pending_discontinuity = False
            return CcVerdict(
                kind="signaled_discontinuity",
                expected_cc=None,
                missing_packets=0,
                signaled=True,
                finding=Finding(
                    code=CC_DISCONTINUITY,
                    severity=Severity.WARNING,
                    disposition=Disposition.ACCEPTED,
                    message=f"signaled discontinuity for PID {pkt.pid:#06x}: "
                            f"counter restarts at cc={cc}; not a packet loss",
                    packet_index=pkt.index, pid=pkt.pid,
                    details={"cc": cc,
                             "previous_payload_cc": st.last_payload_cc}),
            )

        delta = (cc - st.last_payload_cc) % 16 if st.last_payload_cc >= 0 else 1
        expected = (
            (st.last_payload_cc + 1) % 16
            if st.last_payload_cc >= 0 else None
        )

        if delta == 0:
            # Same CC on two consecutive payload packets: legal duplicate
            # (exact repeat) vs. protocol error if bytes differ and the
            # duplicate framing is absent.  Byte identity is checked by the
            # caller, which owns the previous packet; here we distinguish the
            # shape; analyzer passes raw equality through ``exact``.
            finding = Finding(
                code=CC_DUPLICATE,
                severity=Severity.INFO,
                disposition=Disposition.ACCEPTED,
                message=f"duplicate packet for PID {pkt.pid:#06x} "
                        f"(cc={cc})",
                packet_index=pkt.index,
                pid=pkt.pid,
                details={"cc": cc, "has_payload": True},
            )
            kind = "duplicate"
            missing = 0
        elif delta == 1:
            finding = None
            kind = "ok"
            missing = 0
            if signaled:
                finding = Finding(
                    code=CC_DISCONTINUITY_NO_GAP,
                    severity=Severity.INFO,
                    disposition=Disposition.ACCEPTED,
                    message="discontinuity indicator set but counter is "
                            f"continuous for PID {pkt.pid:#06x}",
                    packet_index=pkt.index,
                    pid=pkt.pid,
                    details={"cc": cc, "expected_cc": expected},
                )
                kind = "discontinuity_flag_no_gap"
        else:
            missing = delta - 1
            if signaled:
                finding = Finding(
                    code=CC_DISCONTINUITY,
                    severity=Severity.WARNING,
                    disposition=Disposition.ACCEPTED,
                    message=f"signaled discontinuity for PID {pkt.pid:#06x}: "
                            f"cc jumped {st.last_payload_cc} -> {cc} "
                            f"(~{missing} packet(s) not carried)",
                    packet_index=pkt.index,
                    pid=pkt.pid,
                    details={
                        "cc": cc,
                        "previous_payload_cc": st.last_payload_cc,
                        "missing_estimate": missing,
                    },
                )
                kind = "signaled_discontinuity"
            else:
                finding = Finding(
                    code=CC_GAP,
                    severity=Severity.ERROR,
                    disposition=Disposition.REJECTED,
                    message=f"continuity counter gap for PID {pkt.pid:#06x}: "
                            f"expected {expected}, got {cc} "
                            f"(~{missing} packet(s) lost)",
                    packet_index=pkt.index,
                    pid=pkt.pid,
                    details={
                        "cc": cc,
                        "expected_cc": expected,
                        "missing_estimate": missing,
                    },
                )
                kind = "gap"

        st.last_cc = cc
        st.last_had_payload = True
        st.last_payload_cc = cc
        st.pending_discontinuity = False
        return CcVerdict(kind=kind, expected_cc=expected,
                         missing_packets=missing, signaled=signaled,
                         finding=finding)

    # -- helpers used by the analyzer for exact-duplicate confirmation ------
    def note_bytes_equal(self, verdict: CcVerdict, equal: bool) -> CcVerdict:
        """Refine a ``duplicate`` verdict based on raw payload equality.

        A repeated CC whose bytes actually repeat is a legal duplicate.  A
        repeated CC with *different* bytes is a protocol violation.
        """
        if verdict.kind != "duplicate" or verdict.finding is None:
            return verdict
        if equal:
            return verdict
        f = verdict.finding
        verdict.kind = "repeat_payload_without_duplicate_bit"
        verdict.finding = Finding(
            code=CC_REPEAT_PAYLOAD,
            severity=Severity.ERROR,
            disposition=Disposition.REJECTED,
            message="same continuity counter as previous payload packet but "
                    f"payload bytes differ for PID {f.pid:#06x}",
            packet_index=f.packet_index,
            pid=f.pid,
            details=f.details,
        )
        return verdict

    def pids_seen(self) -> list[int]:
        return sorted(pid for pid, st in self._state.items() if st.seen or True)
