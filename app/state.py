"""State isolation for streaming sessions.

Each session owns an independent :class:`StreamRedactor`, buffer, rule-set
pinning and fragment bookkeeping. Sessions never share buffers or mappings;
a session is pinned to the rule version it started with and rejects chunks
addressed to a different profile (``profile_conflict``).
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

from .kernel import MappingRecord, StreamRedactor
from .rules import RuleSet


class SessionError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass
class Session:
    session_id: str
    ruleset: RuleSet
    redactor: StreamRedactor
    chunk_count: int = 0
    finished: bool = False

    def drain_new_mappings(
        self,
    ) -> tuple[list[MappingRecord], dict[tuple[int, int], str]]:
        return self.redactor.drain_new_mappings()


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, ruleset: RuleSet) -> Session:
        sid = "sess_" + secrets.token_hex(12)
        session = Session(
            session_id=sid,
            ruleset=ruleset,
            redactor=StreamRedactor(ruleset),
        )
        with self._lock:
            self._sessions[sid] = session
        return session

    def get(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise SessionError("session_not_found", f"unknown session {session_id!r}")
        return session

    def pin_check(self, session: Session, ruleset: RuleSet) -> None:
        if session.ruleset.fingerprint != ruleset.fingerprint:
            raise SessionError(
                "profile_conflict",
                f"session pinned to {session.ruleset.version!r}; "
                f"cannot switch to {ruleset.version!r} mid-stream",
            )

    def close(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)
