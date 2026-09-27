"""The jitter-buffer core: reordering, adaptive playout, explicit gaps.

Sequence/timestamp extension
----------------------------
Raw 16-bit sequence numbers and 32-bit timestamps are extended to unbounded
integers. The extension is always taken relative to the *highest sequence
number observed so far*, never relative to arrival order, so a burst that
arrives reversed maps to the same extended values it would have in order.
Timestamps for a sequence are anchored on the highest known seq via a
modular delta plus the per-frame stride; pause/restart gaps (timestamp
jumps larger than one frame) propagate correctly because the anchor is a
real neighbour packet.

Adaptive delay
--------------
RFC 3550 EWMA jitter ``J`` over relative transit, delay
``d = clip(K*J + margin, min_delay, max_delay)`` recomputed per talkspurt
and frozen within it. Playout times are strictly monotonic and exactly
``frame_ms`` apart.

Missing packets are emitted as explicit GAP markers at their deadline with
``audio=None`` — audio content is never synthesised. A packet arriving after
its slot played is LATE_AFTER_PLAYOUT and can never be reinserted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .config import JitterConfig
from .media import RtpPacket
from .time_kernel import ts_to_ms, wrap_delta


class IngestStatus(str, Enum):
    ACCEPTED = "ACCEPTED"                       # buffered, forward seq
    REORDERED = "REORDERED"                     # buffered, older than max seen
    DUPLICATE = "DUPLICATE"                     # seq already buffered
    LATE_AFTER_PLAYOUT = "LATE_AFTER_PLAYOUT"   # slot already played/gapped
    BUFFER_FULL = "BUFFER_FULL"                 # bounded capacity reached


class PlayoutKind(str, Enum):
    AUDIO = "AUDIO"
    GAP = "GAP"


@dataclass
class IngestResult:
    status: IngestStatus
    ssrc: int
    ext_seq: int
    ext_ts: int
    buffer_size: int
    detail: str = ""


@dataclass
class PlayoutItem:
    kind: PlayoutKind
    ssrc: int
    ext_seq: int
    ext_ts: int
    playout_ms: float
    sender_ms: float
    audio: Optional[object] = None
    gap_length_samples: Optional[int] = None
    delay_ms: float = 0.0


@dataclass
class _Buffered:
    packet: RtpPacket
    ext_seq: int
    ext_ts: int
    sender_ms: float


@dataclass
class SessionStats:
    ssrc: int
    accepted: int = 0
    reordered: int = 0
    duplicates: int = 0
    late_after_playout: int = 0
    buffer_full: int = 0
    gaps: int = 0
    played_audio: int = 0
    max_buffer_size: int = 0
    delays_used_ms: List[float] = field(default_factory=list)

    @property
    def min_delay_used_ms(self) -> Optional[float]:
        return min(self.delays_used_ms) if self.delays_used_ms else None

    @property
    def max_delay_used_ms(self) -> Optional[float]:
        return max(self.delays_used_ms) if self.delays_used_ms else None


class JitterSession:
    def __init__(self, ssrc: int, config: JitterConfig, adaptive: bool = True):
        self.ssrc = ssrc
        self.cfg = config
        self.adaptive = adaptive
        self._buf: Dict[int, _Buffered] = {}

        # extension anchors
        self._base_seq: Optional[int] = None
        self._max_ext_seq: Optional[int] = None
        self._ts_anchor_seq: Optional[int] = None  # highest seq with a ts
        self._play_cursor: Optional[int] = None
        # ext_seq -> unwrapped timestamp for every seq ever observed
        self._ext_ts: Dict[int, int] = {}

        # jitter / transit
        self._min_transit: Optional[float] = None
        self._jitter_ms: float = 0.0
        self._q_prev: float = 0.0

        # playout schedule; talkspurt restarts only at genuine boundaries:
        # first packet, after a hole, or an explicit pause/marker boundary.
        self._next_playout_ms: Optional[float] = None
        self._schedule_active = False
        self._talkspurt_delay_ms: float = 0.0
        self._talkspurt_starts: set = {0}

        self._expected_end: Optional[int] = None
        self.stats = SessionStats(ssrc=ssrc)

    # ------------------------------------------------------------------ ingest
    def _extend_ts(self, ext_seq: int, raw_ts: int) -> int:
        """Unwrapped timestamp for ``ext_seq``, anchored on the highest-known.

        The anchor is the greatest seq whose timestamp is already recorded:
        the most recent forward packet. A lower seq arriving out of order is
        extended *backwards* from it. Because the modular delta is taken
        against the anchor's raw timestamp and the seq distance is an exact
        frame count, in-order and reversed bursts map identically; 32-bit
        wrap and pause/restart jumps both propagate.
        """
        anchor = self._ts_anchor_seq
        anchor_raw_ts = self._ext_ts[anchor] & 0xFFFFFFFF
        steps = ext_seq - anchor
        d = wrap_delta(raw_ts, anchor_raw_ts, self.cfg.ts_bits)
        expected = steps * self.cfg.samples_per_packet
        # Use the real modular delta unless it is implausible for the seq
        # distance (> half the timestamp cycle), then stride-extrapolate.
        if abs(d - expected) > (1 << 30):
            value = self._ext_ts[anchor] + expected
        else:
            value = self._ext_ts[anchor] + d
        self._ext_ts[ext_seq] = value
        if ext_seq > self._ts_anchor_seq:
            self._ts_anchor_seq = ext_seq
        return value

    def ingest(self, packet: RtpPacket) -> IngestResult:
        if packet.ssrc != self.ssrc:
            raise ValueError("packet SSRC does not match session")

        if self._base_seq is None:
            self._base_seq = packet.seq
            self._max_ext_seq = 0
            self._ts_anchor_seq = 0
            self._play_cursor = 0
            ext_seq = 0
            ext_ts = packet.timestamp
            self._ext_ts[0] = ext_ts
        else:
            ext_seq = wrap_delta(packet.seq, self._base_seq, self.cfg.seq_bits)
            if ext_seq not in self._ext_ts:
                ext_ts = self._extend_ts(ext_seq, packet.timestamp)
            else:
                ext_ts = self._ext_ts[ext_seq]

        if ext_seq < self._play_cursor:
            self.stats.late_after_playout += 1
            return IngestResult(IngestStatus.LATE_AFTER_PLAYOUT, self.ssrc,
                                ext_seq, ext_ts, len(self._buf),
                                f"seq {ext_seq} < cursor {self._play_cursor}")

        if ext_seq in self._buf:
            self.stats.duplicates += 1
            return IngestResult(IngestStatus.DUPLICATE, self.ssrc,
                                ext_seq, ext_ts, len(self._buf),
                                f"seq {ext_seq} already buffered")

        sender_ms = ts_to_ms(ext_ts, self.cfg.clock_rate)
        transit = packet.arrival_ms - sender_ms
        if self._min_transit is None or transit < self._min_transit:
            self._min_transit = transit
        q = transit - self._min_transit

        if len(self._buf) >= self.cfg.max_buffer_packets:
            self.stats.buffer_full += 1
            return IngestResult(IngestStatus.BUFFER_FULL, self.ssrc,
                                ext_seq, ext_ts, len(self._buf),
                                f"cap={self.cfg.max_buffer_packets}")

        self._buf[ext_seq] = _Buffered(packet, ext_seq, ext_ts, sender_ms)
        self.stats.accepted += 1

        # High-water mark (all-time highest seq observed). A new packet below
        # it is, by definition, out of order: this counts every packet inside
        # a reversed burst, not just those below the immediately prior arrival.
        is_reorder = ext_seq < self._max_ext_seq
        if ext_seq > self._max_ext_seq:
            self._max_ext_seq = ext_seq

        if is_reorder:
            self.stats.reordered += 1
            status = IngestStatus.REORDERED
        else:
            # detect pause/restart via the immediately preceding real packet
            prev = ext_seq - 1
            if prev in self._ext_ts:
                gap_ticks = ext_ts - self._ext_ts[prev]
                if packet.marker or gap_ticks > self.cfg.samples_per_packet:
                    self._talkspurt_starts.add(ext_seq)
            status = IngestStatus.ACCEPTED

        self.stats.max_buffer_size = max(self.stats.max_buffer_size, len(self._buf))

        # RFC 3550 EWMA jitter, arrival order (q uses global min transit)
        if self.stats.accepted == 1:
            self._jitter_ms = 0.0
        else:
            self._jitter_ms += (abs(q - self._q_prev) - self._jitter_ms) / 16.0
        self._q_prev = q

        return IngestResult(status, self.ssrc, ext_seq, ext_ts, len(self._buf))

    def close_stream(self, last_ext_seq: Optional[int] = None) -> None:
        if last_ext_seq is None:
            last_ext_seq = (self._max_ext_seq if self._max_ext_seq is not None
                            else 0)
        self._expected_end = last_ext_seq

    # ------------------------------------------------------------------ delay
    def _current_delay_ms(self) -> float:
        if not self.adaptive:
            return self.cfg.fixed_delay_ms
        raw = self.cfg.jitter_multiplier * self._jitter_ms + self.cfg.safety_margin_ms
        return min(self.cfg.max_delay_ms, max(self.cfg.min_delay_ms, raw))

    def _start_talkspurt(self) -> None:
        d = self._current_delay_ms()
        self._talkspurt_delay_ms = d
        self.stats.delays_used_ms.append(d)

        head = self._buf.get(self._play_cursor)
        if head is not None:
            ideal = head.sender_ms + self._min_transit + d
        else:
            future = sorted(s for s in self._buf if s > self._play_cursor)
            if future:
                nxt = self._buf[future[0]]
                steps = future[0] - self._play_cursor
                ideal = (nxt.sender_ms + self._min_transit + d
                         - steps * self.cfg.frame_ms)
            elif self._next_playout_ms is not None:
                ideal = self._next_playout_ms
            else:
                ideal = 0.0

        if self._next_playout_ms is None:
            anchor = ideal
        else:
            # keep the output clock on its strict frame grid: never rewind,
            # never use a non-multiple spacing, regardless of sender skew
            grid = self._next_playout_ms
            if ideal > grid:
                # advance by a whole number of frames up to the ideal target
                steps_back = max(1, int(round(
                    (ideal - grid) / self.cfg.frame_ms)))
                anchor = grid + steps_back * self.cfg.frame_ms
            else:
                anchor = grid
        self._next_playout_ms = anchor
        self._schedule_active = True

    def _gap_ext_ts(self) -> int:
        nxt = self._buf.get(self._play_cursor + 1)
        if nxt is not None:
            return nxt.ext_ts - self.cfg.samples_per_packet
        prev = self._play_cursor - 1
        if prev in self._ext_ts:
            return self._ext_ts[prev] + self.cfg.samples_per_packet
        return self._play_cursor * self.cfg.samples_per_packet

    # ------------------------------------------------------------------ drain
    def drain(self, now_ms: float) -> List[PlayoutItem]:
        out: List[PlayoutItem] = []
        if self._play_cursor is None:
            return out

        while True:
            head = self._buf.get(self._play_cursor)
            in_range = (self._expected_end is not None
                        and self._play_cursor <= self._expected_end)

            if not self._schedule_active:
                if head is None:
                    # Confirm a missing head as a hole when EITHER the stream
                    # was closed within its declared span, OR the highest seq
                    # observed is far enough ahead that this cannot be an
                    # ordinary adjacent jitter reorder. In both cases the
                    # slot's own playout deadline must also have elapsed.
                    deadline_passed = (self._next_playout_ms is not None
                                       and now_ms + 1e-9 >= self._next_playout_ms)
                    gap_confirmed_by_seq = (
                        self._max_ext_seq is not None
                        and self._max_ext_seq - self._play_cursor
                        >= self.cfg.late_packet_threshold)
                    if deadline_passed and (in_range or gap_confirmed_by_seq):
                        self._schedule_active = True
                    else:
                        break
                else:
                    self._start_talkspurt()

            if now_ms + 1e-9 < self._next_playout_ms:
                break

            if head is not None:
                out.append(PlayoutItem(
                    kind=PlayoutKind.AUDIO, ssrc=self.ssrc,
                    ext_seq=head.ext_seq, ext_ts=head.ext_ts,
                    playout_ms=self._next_playout_ms, sender_ms=head.sender_ms,
                    audio=head.packet.decode_audio(),
                    delay_ms=self._talkspurt_delay_ms))
                del self._buf[head.ext_seq]
                self.stats.played_audio += 1
                self._advance()
                if self._play_cursor in self._talkspurt_starts:
                    self._schedule_active = False
                continue

            # head missing while schedule active and deadline passed
            confirmed = in_range or (
                self._max_ext_seq is not None
                and self._max_ext_seq - self._play_cursor
                >= self.cfg.late_packet_threshold)
            if not confirmed:
                # Not yet distinguishable from adjacent jitter: keep waiting.
                self._schedule_active = False
                break

            ext_ts_missing = self._gap_ext_ts()
            out.append(PlayoutItem(
                kind=PlayoutKind.GAP, ssrc=self.ssrc,
                ext_seq=self._play_cursor, ext_ts=ext_ts_missing,
                playout_ms=self._next_playout_ms,
                sender_ms=ts_to_ms(ext_ts_missing, self.cfg.clock_rate),
                audio=None, gap_length_samples=self.cfg.samples_per_packet,
                delay_ms=self._talkspurt_delay_ms))
            self.stats.gaps += 1
            self._advance()
            # A real packet following a gap starts a fresh talkspurt.
            if self._play_cursor in self._buf:
                self._schedule_active = False
            else:
                # keep the gap chain alive only while the hole is still
                # confirmed (declared stream span or seq-span threshold)
                still_in_range = (self._expected_end is not None
                                  and self._play_cursor <= self._expected_end)
                still_by_seq = (
                    self._max_ext_seq is not None
                    and self._max_ext_seq - self._play_cursor
                    >= self.cfg.late_packet_threshold)
                if not (still_in_range or still_by_seq):
                    self._schedule_active = False

        return out

    def _advance(self) -> None:
        self._play_cursor += 1
        self._next_playout_ms += self.cfg.frame_ms

    @property
    def buffered_count(self) -> int:
        return len(self._buf)

    @property
    def jitter_ms(self) -> float:
        return self._jitter_ms

    @property
    def play_cursor(self) -> Optional[int]:
        return self._play_cursor
