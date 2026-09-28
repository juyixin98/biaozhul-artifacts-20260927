"""路由：版本、一次性匹配、会话流、命中分页、诊断、健康检查。

所有写路径在 deps.lock 下串行执行（本地单进程服务 + check_same_thread=False
的单个 SQLite 连接）。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query

from ..text_spec import decode_payload
from .deps import AppState, get_state
from .schemas import (
    CreateVersionIn,
    FeedIn,
    MatchIn,
    OpenSessionIn,
    SwitchVersionIn,
)

router = APIRouter()


def _decode_patterns(
    encoding: str, patterns
) -> list[tuple[str, bytes]]:
    """按版本/请求级编码解码全部模式；失败抛 ENCODING_ERROR（422）。"""
    return [
        (p.id, decode_payload(p.data, encoding, field=f"patterns[{p.id}]"))
        for p in patterns
    ]


# ---------------------------------------------------------------------- 健康

@router.get("/health", tags=["meta"])
def health(state: AppState = Depends(get_state)) -> dict:
    return {"status": "ok", "version": "1.0.0", "db": str(state.settings.db_path)}


# -------------------------------------------------------------------- 版本集

@router.post("/versions", status_code=201, tags=["versions"])
def create_version(
    payload: CreateVersionIn, state: AppState = Depends(get_state)
) -> dict:
    patterns = _decode_patterns(payload.encoding, payload.patterns)
    with state.lock:
        version_id, automaton = state.registry.create(patterns, payload.encoding)
    return {
        "version_id": version_id,
        "fingerprint": automaton.fingerprint,
        "pattern_count": automaton.pattern_count,
        "node_count": automaton.node_count,
    }


@router.get("/versions", tags=["versions"])
def list_versions(state: AppState = Depends(get_state)) -> dict:
    with state.lock:
        rows = state.registry.list_versions()
    return {
        "items": [
            {
                "version_id": r["version_id"],
                "fingerprint": r["fingerprint"],
                "pattern_count": r["pattern_count"],
                "encoding": r["encoding"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------- 一次性匹配

@router.post("/match", tags=["match"])
def oneshot_match(payload: MatchIn, state: AppState = Depends(get_state)) -> dict:
    patterns = _decode_patterns(payload.encoding, payload.patterns)
    data = decode_payload(payload.data, payload.encoding, field="data")
    with state.lock:
        return state.service.oneshot(patterns, data, payload.encoding)


# ------------------------------------------------------------------ 流会话

@router.post("/sessions", status_code=201, tags=["sessions"])
def open_session(
    payload: OpenSessionIn, state: AppState = Depends(get_state)
) -> dict:
    with state.lock:
        return state.service.open_session(payload.version_id)


@router.post("/sessions/{sid}/feed", tags=["sessions"])
def feed_session(
    sid: str, payload: FeedIn, state: AppState = Depends(get_state)
) -> dict:
    chunk = decode_payload(payload.data, payload.encoding, field="data")
    with state.lock:
        return state.service.feed(
            sid,
            chunk,
            expected_offset=payload.expected_offset,
            expected_fingerprint=payload.expected_fingerprint,
            finish=payload.finish,
        )


@router.post("/sessions/{sid}/switch-version", tags=["sessions"])
def switch_version(
    sid: str, payload: SwitchVersionIn, state: AppState = Depends(get_state)
) -> dict:
    with state.lock:
        return state.service.switch_version(sid, payload.version_id)


@router.post("/sessions/{sid}/finish", tags=["sessions"])
def finish_session(sid: str, state: AppState = Depends(get_state)) -> dict:
    with state.lock:
        # 复用 feed 的校验与结束路径：零字节块 + finish=true，不改变偏移/节点。
        return state.service.feed(
            sid,
            b"",
            expected_offset=None,
            expected_fingerprint=None,
            finish=True,
        )


@router.get("/sessions/{sid}/state", tags=["sessions"])
def session_state(sid: str, state: AppState = Depends(get_state)) -> dict:
    with state.lock:
        return state.service.get_state(sid)


@router.get("/sessions/{sid}/hits", tags=["sessions"])
def session_hits(
    sid: str,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    state: AppState = Depends(get_state),
) -> dict:
    with state.lock:
        return state.service.list_hits_page(sid, cursor=cursor, limit=limit)


# ---------------------------------------------------------------------- 诊断

@router.get("/diagnostics", tags=["diagnostics"])
def list_diagnostics(
    sid: str | None = Query(default=None),
    request_id: str | None = Query(default=None),
    outcome: str | None = Query(
        default=None, pattern="^(accept|reject|undetermined)$"
    ),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    state: AppState = Depends(get_state),
) -> dict:
    with state.lock:
        rows = state.diagnostics_store.list_events(
            sid=sid, request_id=request_id, outcome=outcome, limit=limit, offset=offset
        )
        total = state.diagnostics_store.count_events(
            sid=sid, request_id=request_id, outcome=outcome
        )
    return {
        "total": total,
        "items": [
            {
                "event_id": r["event_id"],
                "request_id": r["request_id"],
                "sid": r["sid"],
                "version_id": r["version_id"],
                "outcome": r["outcome"],
                "code": r["code"],
                "message": r["message"],
                "key_state": json.loads(r["key_state"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }
