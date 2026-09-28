"""匹配编排服务：会话生命周期、流式喂入、边界切换、分页命中、一次性匹配。

职责边界：本层负责协议级校验、事务、状态推进与诊断记录；
字节级匹配算法全部在 automaton/stream 内，分页令牌在 cursor 内。
"""

from __future__ import annotations

import sqlite3

from ..cursor import decode_cursor, encode_cursor
from ..diagnostics import Diagnostics
from ..errors import ApiError, ErrorCode
from ..storage.db import HitStore, SessionStore, new_id, transaction
from ..stream import StreamMatcher
from .version_registry import VersionRegistry


class MatcherService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: VersionRegistry,
        diagnostics: Diagnostics,
        *,
        cursor_secret: bytes,
        default_page_size: int = 200,
        max_page_size: int = 10_000,
        oneshot_cap: int = 10_000,
    ) -> None:
        self._conn = conn
        self._sessions = SessionStore(conn)
        self._hits = HitStore(conn)
        self._registry = registry
        self._diag = diagnostics
        self._secret = cursor_secret
        self._default_page_size = default_page_size
        self._max_page_size = max_page_size
        self._oneshot_cap = oneshot_cap

    # ------------------------------------------------------------- 会话生命周期

    def open_session(self, version_id: str) -> dict:
        automaton = self._registry.get(version_id)  # 不存在则 VERSION_NOT_FOUND
        sid = new_id("sess")
        with transaction(self._conn):
            self._sessions.create(sid, version_id, automaton.fingerprint)
        self._diag.record(
            outcome="accept",
            code="SESSION_OPENED",
            message="会话已打开，自动机处于根节点，流偏移为 0",
            key_state={
                "node_state": 0,
                "byte_offset": 0,
                "fingerprint": automaton.fingerprint,
                "pattern_count": automaton.pattern_count,
            },
            sid=sid,
            version_id=version_id,
        )
        return {
            "sid": sid,
            "version_id": version_id,
            "fingerprint": automaton.fingerprint,
            "state": {"node_state": 0, "byte_offset": 0, "feed_count": 0},
            "status": "open",
        }

    # ------------------------------------------------------------------- 喂入

    def feed(
        self,
        sid: str,
        chunk: bytes,
        *,
        expected_offset: int | None,
        expected_fingerprint: str | None,
        finish: bool,
    ) -> dict:
        row = self._require_session(sid)
        self._assert_open(row)

        if expected_offset is not None and expected_offset != row["byte_offset"]:
            # 客户端对当前偏移的认知与服务端不一致：本块未被消费，状态不变。
            # 诊断记录由全局异常处理器统一落库（outcome=undetermined）。
            raise ApiError(
                ErrorCode.OFFSET_MISMATCH,
                409,
                "expected_offset 与服务端当前偏移不一致，本块未消费；"
                "可用 GET /sessions/{sid}/state 核对后重试",
                details={
                    "server_offset": row["byte_offset"],
                    "expected_offset": expected_offset,
                    "chunk_len": len(chunk),
                    "node_state": row["node_state"],
                },
                outcome="undetermined",
            )

        if (
            expected_fingerprint is not None
            and expected_fingerprint != row["fingerprint"]
        ):
            raise ApiError(
                ErrorCode.FINGERPRINT_MISMATCH,
                409,
                "expected_fingerprint 与会话当前版本不一致；请在显式边界调用 "
                "switch-version 后再继续",
                details={
                    "server_fingerprint": row["fingerprint"],
                    "expected_fingerprint": expected_fingerprint,
                    "node_state": row["node_state"],
                    "byte_offset": row["byte_offset"],
                },
                outcome="undetermined",
            )

        automaton = self._registry.get(row["version_id"])
        # 双保险：缓存/重建得到的自动机必须和会话绑定的指纹一致，否则节点号不能用。
        if automaton.fingerprint != row["fingerprint"]:
            raise ApiError(
                ErrorCode.FINGERPRINT_MISMATCH,
                409,
                "自动机指纹与会话绑定不一致，已拒绝推进",
                details={
                    "automaton_fingerprint": automaton.fingerprint,
                    "session_fingerprint": row["fingerprint"],
                    "node_state": row["node_state"],
                },
                outcome="undetermined",
            )

        matcher = StreamMatcher(
            automaton, state=row["node_state"], offset=row["byte_offset"]
        )
        feed_seq = row["feed_count"] + 1
        hits = matcher.feed(chunk)

        hit_rows = [(h.end, h.start, h.pattern_id) for h in hits]
        with transaction(self._conn):
            self._hits.add_many(sid, feed_seq, hit_rows)
            self._sessions.advance_feed(
                sid,
                node_state=matcher.state,
                byte_offset=matcher.offset,
            )
            total = self._hits.count(sid)
            if finish:
                self._sessions.finish(sid)
            fresh = self._sessions.get(sid)

        self._diag.record(
            outcome="accept",
            code="CHUNK_ACCEPTED",
            message=(
                f"块已消费：{len(chunk)} 字节，本块新增命中 {len(hits)}"
                + ("；会话已结束" if finish else "")
            ),
            key_state={
                "feed_seq": feed_seq,
                "chunk_len": len(chunk),
                "base_offset": row["byte_offset"],
                "new_offset": matcher.offset,
                "node_state_before": row["node_state"],
                "node_state_after": matcher.state,
                "new_hits": len(hits),
                "total_hits": total,
                "chunk": chunk,  # bytes 会被诊断层按策略脱敏
            },
            sid=sid,
            version_id=row["version_id"],
        )
        return {
            "sid": sid,
            "new_hits": len(hits),
            "total_hits": total,
            "state": {
                "node_state": fresh["node_state"],
                "byte_offset": fresh["byte_offset"],
                "feed_count": fresh["feed_count"],
            },
            "status": fresh["status"],
            "fingerprint": fresh["fingerprint"],
        }

    # ------------------------------------------------------------- 显式版本边界

    def switch_version(self, sid: str, new_version_id: str) -> dict:
        row = self._require_session(sid)
        self._assert_open(row)
        automaton = self._registry.get(new_version_id)  # 不存在则 404

        if new_version_id == row["version_id"]:
            self._diag.record(
                outcome="accept",
                code="VERSION_SWITCH_NOOP",
                message="目标版本与当前版本相同，仍在显式边界重置节点为根",
                key_state={"byte_offset": row["byte_offset"]},
                sid=sid,
                version_id=new_version_id,
            )
            with transaction(self._conn):
                self._sessions.switch_version(
                    sid,
                    version_id=new_version_id,
                    fingerprint=automaton.fingerprint,
                    node_state=0,
                )
                fresh = self._sessions.get(sid)
            return self._state_payload(fresh)

        with transaction(self._conn):
            self._sessions.switch_version(
                sid,
                version_id=new_version_id,
                fingerprint=automaton.fingerprint,
                node_state=0,  # 显式边界：旧自动机节点一律丢弃，从新 trie 的根开始
            )
            fresh = self._sessions.get(sid)

        self._diag.record(
            outcome="accept",
            code="VERSION_SWITCHED",
            message="已在显式边界切换自动机：节点重置为根，流偏移保留；跨边界不产生命中",
            key_state={
                "old_version_id": row["version_id"],
                "new_version_id": new_version_id,
                "old_fingerprint": row["fingerprint"],
                "new_fingerprint": automaton.fingerprint,
                "node_state": 0,
                "byte_offset": fresh["byte_offset"],
            },
            sid=sid,
            version_id=new_version_id,
        )
        return self._state_payload(fresh)

    # ------------------------------------------------------------------- 查询

    def get_state(self, sid: str) -> dict:
        return self._state_payload(self._require_session(sid))

    def list_hits_page(
        self, sid: str, *, cursor: str | None, limit: int | None
    ) -> dict:
        self._require_session(sid)
        page_size = self._resolve_limit(limit)

        after = None
        if cursor:
            key = decode_cursor(self._secret, cursor, session_id=sid)
            after = (key["end"], key["start"], key["pid"], key["seq"])

        # 多取 1 行判断是否还有下一页。
        rows = self._hits.page(sid, limit=page_size + 1, after=after)
        has_more = len(rows) > page_size
        rows = rows[:page_size]

        items = [
            {
                "pattern_id": r["pattern_id"],
                "start": r["start_offset"],
                "end": r["end_offset"],
                "feed_seq": r["feed_seq"],
            }
            for r in rows
        ]
        next_cursor = None
        if has_more:
            last = rows[-1]
            next_cursor = encode_cursor(
                self._secret,
                session_id=sid,
                end=last["end_offset"],
                start=last["start_offset"],
                pattern_id=last["pattern_id"],
                seq=last["feed_seq"],
            )
        return {
            "sid": sid,
            "items": items,
            "page_size": page_size,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    # --------------------------------------------------------------- 一次性匹配

    def oneshot(self, patterns: list[tuple[str, bytes]], data: bytes, encoding: str) -> dict:
        """无状态便利接口：构建自动机（或命中幂等版本）+ 根状态一次匹配。"""
        version_id, automaton = self._registry.create(patterns, encoding)
        hits = automaton.search(data)
        truncated = False
        if len(hits) > self._oneshot_cap:
            hits = hits[: self._oneshot_cap]
            truncated = True
        items = [
            {"pattern_id": h.pattern_id, "start": h.start, "end": h.end}
            for h in hits
        ]
        self._diag.record(
            outcome="accept",
            code="ONESHOT_MATCH",
            message="一次性匹配完成（无会话、无跨块状态）",
            key_state={
                "version_id": version_id,
                "pattern_count": automaton.pattern_count,
                "data_len": len(data),
                "hit_count": len(items),
                "truncated": truncated,
                "data": data,
            },
            version_id=version_id,
        )
        return {
            "version_id": version_id,
            "fingerprint": automaton.fingerprint,
            "total_hits": len(items),
            "truncated": truncated,
            "hits": items,
        }

    # ------------------------------------------------------------------ 辅助

    def _require_session(self, sid: str) -> sqlite3.Row:
        row = self._sessions.get(sid)
        if row is None:
            raise ApiError(
                ErrorCode.SESSION_NOT_FOUND,
                404,
                f"会话 {sid} 不存在",
                details={"sid": sid},
            )
        return row

    @staticmethod
    def _assert_open(row: sqlite3.Row) -> None:
        if row["status"] == "finished":
            raise ApiError(
                ErrorCode.SESSION_FINISHED,
                409,
                f"会话 {row['sid']} 已结束，不能再喂入或切换版本；"
                "历史命中仍可分页读取",
                details={"sid": row["sid"]},
            )

    def _resolve_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._default_page_size
        if limit <= 0:
            raise ApiError(
                ErrorCode.LIMIT_INVALID,
                422,
                "limit 必须为正整数",
                details={"limit": limit},
            )
        if limit > self._max_page_size:
            raise ApiError(
                ErrorCode.LIMIT_INVALID,
                422,
                f"limit 超过上限 {self._max_page_size}",
                details={"limit": limit, "max": self._max_page_size},
            )
        return limit

    @staticmethod
    def _state_payload(row: sqlite3.Row) -> dict:
        return {
            "sid": row["sid"],
            "version_id": row["version_id"],
            "fingerprint": row["fingerprint"],
            "state": {
                "node_state": row["node_state"],
                "byte_offset": row["byte_offset"],
                "feed_count": row["feed_count"],
            },
            "status": row["status"],
        }
