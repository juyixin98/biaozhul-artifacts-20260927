"""请求级状态隔离。

每个请求（整段或流式会话）拥有独立的 request_id、redactor 实例、
规则档快照名/版本与开始时间；请求之间不共享任何可变状态。
"""
from __future__ import annotations

import itertools
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..core.redactor import EmittedChunk, RedactionResult, StreamingRedactor
from ..rules.models import Profile

_counter = itertools.count(1)
_counter_lock = threading.Lock()


def new_request_id() -> str:
    with _counter_lock:
        seq = next(_counter)
    return f"req-{uuid.uuid4().hex[:12]}-{seq:06d}"


@dataclass
class RequestState:
    request_id: str
    profile_name: str
    profile_version: str
    mode: str  # "whole" | "stream"
    redactor: StreamingRedactor
    chunks_received: int = 0
    chunks_emitted: list[EmittedChunk] = field(default_factory=list)
    finalized: bool = False
    result: RedactionResult | None = None
    sink_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def open(self) -> bool:
        return not self.finalized

    def snapshot_key(self) -> str:
        return f"{self.profile_name}@{self.profile_version}"


class SessionRegistry:
    """仅维护 request_id -> RequestState 的映射，进程内、带锁。

    不持有任何原文；原文只存在于对应请求的 redactor 缓冲内，
    finalize 后缓冲清空。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, RequestState] = {}

    def create(self, profile: Profile, mode: str) -> RequestState:
        rid = new_request_id()

        def sink(evt: Any) -> None:
            state.sink_events.append({"kind": evt.kind, **evt.payload})

        state = RequestState(
            request_id=rid,
            profile_name=profile.name,
            profile_version=profile.version,
            mode=mode,
            redactor=StreamingRedactor(profile, sink=sink),
        )
        with self._lock:
            self._sessions[rid] = state
        return state

    def get(self, request_id: str) -> RequestState | None:
        with self._lock:
            return self._sessions.get(request_id)

    def drop(self, request_id: str) -> None:
        with self._lock:
            self._sessions.pop(request_id, None)

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for s in self._sessions.values() if s.open)
