"""Per-SSRC session registry: an SSRC change creates an independent session."""
from __future__ import annotations

from typing import Dict, Iterable, List

from .config import JitterConfig
from .jitter import IngestResult, JitterSession, PlayoutItem
from .media import RtpPacket


class SessionRegistry:
    def __init__(self, config: JitterConfig, adaptive: bool = True):
        self.cfg = config
        self.adaptive = adaptive
        self._sessions: Dict[int, JitterSession] = {}
        self._order: List[int] = []

    def ingest(self, packet: RtpPacket) -> IngestResult:
        session = self._sessions.get(packet.ssrc)
        if session is None:
            session = JitterSession(packet.ssrc, self.cfg, adaptive=self.adaptive)
            self._sessions[packet.ssrc] = session
            self._order.append(packet.ssrc)
        return session.ingest(packet)

    def session(self, ssrc: int) -> JitterSession:
        return self._sessions[ssrc]

    def close_all(self) -> None:
        for s in self._sessions.values():
            s.close_stream()

    def drain(self, now_ms: float) -> List[PlayoutItem]:
        items: List[PlayoutItem] = []
        for ssrc in self._order:
            items.extend(self._sessions[ssrc].drain(now_ms))
        items.sort(key=lambda it: (it.playout_ms, it.ssrc, it.ext_seq))
        return items

    @property
    def ssrcs(self) -> List[int]:
        return list(self._order)

    @property
    def sessions(self) -> Iterable[JitterSession]:
        return [self._sessions[s] for s in self._order]
