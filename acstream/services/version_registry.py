"""模式版本注册表：版本的创建、查找与自动机缓存。

- 版本内容不可变；同一模式多重集（指纹相同）复用同一版本；
- Automaton 构建后缓存在内存；进程重启后按需从 SQLite 重建，
  并校验重建指纹，防止持久化数据与索引语义漂移；
- 自动机构建失败（空模式、重复 id）在事务内整体回滚，不产生半成品版本。
"""

from __future__ import annotations

import sqlite3

from ..automaton import Automaton, Pattern
from ..storage.db import VersionStore, new_id, transaction


class VersionRegistry:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._versions = VersionStore(conn)
        self._cache: dict[str, Automaton] = {}

    def create(self, patterns: list[tuple[str, bytes]], encoding: str) -> tuple[str, Automaton]:
        # 先构建自动机：空模式/重复 id 在写库之前即被拒绝。
        automaton = Automaton([Pattern(pid, data) for pid, data in patterns])
        fingerprint = automaton.fingerprint

        with transaction(self._conn):
            existing = self._versions.exists_by_fingerprint(fingerprint)
            if existing is not None:
                # 幂等：同内容复用既有版本。
                self._cache[existing] = automaton
                return existing, automaton

            version_id = new_id("ver")
            self._versions.create(version_id, fingerprint, encoding, patterns)
        self._cache[version_id] = automaton
        return version_id, automaton

    def get(self, version_id: str) -> Automaton:
        cached = self._cache.get(version_id)
        if cached is not None:
            return cached

        row = self._versions.get(version_id)
        if row is None:
            from ..errors import ApiError, ErrorCode

            raise ApiError(
                ErrorCode.VERSION_NOT_FOUND,
                404,
                f"模式版本 {version_id} 不存在",
                details={"version_id": version_id},
            )

        patterns = self._versions.list_patterns(version_id)
        automaton = Automaton([Pattern(pid, data) for pid, data in patterns])
        if automaton.fingerprint != row["fingerprint"]:
            # 无法判定：持久化内容重建出的自动机与记录指纹不一致，
            # 继续使用会让节点状态语义失配——拒绝服务该版本。
            from ..errors import ApiError, ErrorCode

            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                500,
                "版本重建指纹与存储指纹不一致，自动机索引可能已损坏",
                details={
                    "version_id": version_id,
                    "stored_fingerprint": row["fingerprint"],
                    "rebuilt_fingerprint": automaton.fingerprint,
                },
                outcome="undetermined",
            )
        self._cache[version_id] = automaton
        return automaton

    def fingerprint_of(self, version_id: str) -> str:
        row = self._versions.get(version_id)
        if row is None:
            from ..errors import ApiError, ErrorCode

            raise ApiError(
                ErrorCode.VERSION_NOT_FOUND,
                404,
                f"模式版本 {version_id} 不存在",
                details={"version_id": version_id},
            )
        return row["fingerprint"]

    def list_versions(self) -> list[sqlite3.Row]:
        return self._versions.list_versions()
