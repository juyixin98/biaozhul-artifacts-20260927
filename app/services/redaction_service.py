"""脱敏编排服务：整段与流式共用，负责状态隔离与审计落库。"""
from __future__ import annotations

from typing import Any

from ..core.redactor import EmittedChunk, RedactionResult
from ..rules.models import Profile
from ..rules.parser import Registry
from ..state.audit_store import AuditError, AuditStore
from ..state.logging_utils import SAFE_LOGGER
from ..state.sessions import RequestState, SessionRegistry


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class RedactionService:
    def __init__(self, registry: Registry, store: AuditStore,
                 max_chars: int = 200_000) -> None:
        self._registry = registry
        self._store = store
        self._sessions = SessionRegistry()
        self._max_chars = max_chars

    # ------------------------------------------------------------------ #
    @property
    def sessions(self) -> SessionRegistry:
        return self._sessions

    def _profile(self, name: str | None) -> Profile:
        try:
            return self._registry.get(name)
        except KeyError:
            raise ServiceError(
                "UNKNOWN_PROFILE",
                f"规则档 {name!r} 不存在；可选: {sorted(self._registry.profiles)}",
                http_status=404)

    @staticmethod
    def _check_size(total: int, limit: int) -> None:
        if total > limit:
            raise ServiceError(
                "INPUT_TOO_LARGE",
                f"累计输入 {total} 字符超过上限 {limit}", http_status=413)

    # ------------------------------------------------------------------ #
    def redact_whole(self, text: str, profile_name: str | None) -> RequestState:
        profile = self._profile(profile_name)
        self._check_size(len(text), self._max_chars)
        state = self._sessions.create(profile, mode="whole")
        try:
            self._store.create_request(
                state.request_id, profile.name, profile.version,
                _engine_version(), "whole")
        except AuditError:
            self._sessions.drop(state.request_id)
            raise
        state.chunks_received = 1
        try:
            self._store.record_chunk(state.request_id)
        except AuditError:
            pass
        SAFE_LOGGER.info(
            "whole request=%s profile=%s len=%d",
            state.request_id, profile.name, len(text))
        emitted = state.redactor.feed(text)
        state.chunks_emitted.append(emitted)
        result = state.redactor.finalize()
        self._finish(state, result)
        return state

    # ------------------------------------------------------------------ #
    def open_stream(self, profile_name: str | None) -> RequestState:
        profile = self._profile(profile_name)
        state = self._sessions.create(profile, mode="stream")
        self._store.create_request(
            state.request_id, profile.name, profile.version,
            _engine_version(), "stream")
        SAFE_LOGGER.info("stream open request=%s profile=%s",
                         state.request_id, profile.name)
        return state

    def feed_stream(self, request_id: str, chunk: str,
                    is_final: bool = False) -> tuple[RequestState,
                                                     EmittedChunk | None,
                                                     RedactionResult | None]:
        state = self._require_open(request_id)
        self._check_size(state.redactor.total_received + len(chunk),
                         self._max_chars)
        state.chunks_received += 1
        try:
            self._store.record_chunk(state.request_id)
        except AuditError:
            pass
        emitted = state.redactor.feed(chunk)
        state.chunks_emitted.append(emitted)
        result: RedactionResult | None = None
        if is_final:
            result = state.redactor.finalize()
            self._finish(state, result)
        return state, emitted, result

    def finalize_stream(self, request_id: str) -> RequestState:
        state = self._require_open(request_id)
        result = state.redactor.finalize()
        self._finish(state, result)
        return state

    # ------------------------------------------------------------------ #
    def _require_open(self, request_id: str) -> RequestState:
        state = self._sessions.get(request_id)
        if state is None:
            raise ServiceError("SESSION_NOT_FOUND",
                               f"会话 {request_id} 不存在或已关闭", 404)
        if state.finalized:
            raise ServiceError("SESSION_CLOSED",
                               f"会话 {request_id} 已结束", 409)
        return state

    def _finish(self, state: RequestState, result: RedactionResult) -> None:
        state.finalized = True
        state.result = result
        try:
            self._store.finalize_request(
                state.request_id, result, state.chunks_received,
                state.sink_events)
        except AuditError as exc:
            SAFE_LOGGER.error("audit persist failed request=%s: %s",
                              state.request_id, exc.code if hasattr(exc, "code")
                              else "AUDIT_ERROR")
        SAFE_LOGGER.info(
            "finalized request=%s status=%s mappings=%d uncertainties=%d "
            "residual=%d error_code=%s",
            state.request_id, result.status, len(result.mappings),
            len(result.uncertainties), len(result.residual_findings),
            result.error_code or "-")

    def get_state(self, request_id: str) -> RequestState:
        state = self._sessions.get(request_id)
        if state is None:
            raise ServiceError("SESSION_NOT_FOUND",
                               f"会话 {request_id} 不存在", 404)
        return state


def _engine_version() -> str:
    from .. import __version__
    return __version__
