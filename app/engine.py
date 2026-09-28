"""领域服务层：绑定版本存储与压缩 Trie 索引。

职责
====

- 启动时从 SQLite 的 ``entries`` 全量重建内存 Trie；
- 写操作：规范化 → 事务提交 → 在锁内更新 Trie（先提交后改索引；
  若索引更新失败则置为 degraded 并报 500，绝不在索引与存储不一致时
  继续对外提供可能错误的结果）；
- 读操作：在当前 Trie 上做 best-first 精确 top-k，不遍历全词典排序；
- 历史查询：对已物化的快照版本在只读 Trie 上查询（带 LRU 缓存）；
- 诊断：独立重算 Trie 不变量并报告结构/上界违规。
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .errors import (
    EntryNotFound,
    IndexDegraded,
    NormalizerMismatch,
    ValidationFailure,
    VersionNotFound,
)
from .normalize import NORMALIZER_VERSION, normalize_text
from .storage import Storage, connect, new_client_batch_id
from .trie import RadixTrie, TopKTrace

_SNAPSHOT_CACHE_MAX = 4


def _validate_score(score: float, field: str = "score") -> float:
    """分值必须是有限实数；NaN 与 ±inf 一律拒绝（不允许未知状态混入排序）。"""
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValidationFailure(f"{field} 必须是数值", field=field)
    f = float(score)
    if math.isnan(f) or math.isinf(f):
        raise ValidationFailure(f"{field} 必须是有限实数，不能为 NaN/Infinity", field=field)
    return f


def _validate_term(term: object) -> str:
    if not isinstance(term, str) or term == "":
        raise ValidationFailure("term 必须是非空字符串", field="term")
    return term


class CompletionResult:
    """查询结果（含可选剪枝轨迹）。"""

    def __init__(
        self,
        rows: list[tuple[str, str, str, float]],
        prefix: str,
        prefix_norm: str,
        version: int,
        trace: Optional[TopKTrace],
    ) -> None:
        self.rows = rows
        self.prefix = prefix
        self.prefix_norm = prefix_norm
        self.version = version
        self.trace = trace


class Engine:
    """单实例服务对象；多线程下以可重入锁串行化变更。"""

    def __init__(self, db_path: str | Path, topk_max: int = 100) -> None:
        self.db_path = str(db_path)
        self.topk_max = topk_max
        self.storage = Storage(connect(self.db_path))
        if self.storage.normalizer_version != NORMALIZER_VERSION:
            # 数据库由不兼容的规范化版本创建：拒绝启动，避免静默重解释历史数据。
            raise NormalizerMismatch(
                f"数据库规范化版本 {self.storage.normalizer_version!r} 与代码"
                f" {NORMALIZER_VERSION!r} 不兼容"
            )
        self._lock = threading.RLock()
        self._degraded = False
        self.trie = RadixTrie()
        self._rebuild_locked()
        self._snapshot_cache: "OrderedDict[int, RadixTrie]" = OrderedDict()

    # ----------------------------------------------------------------- 内部

    def _rebuild_locked(self) -> int:
        """从存储全量重建 Trie（按 id 排序，构建过程确定性）。"""
        trie = RadixTrie()
        count = 0
        for row in self.storage.iter_entries():
            trie.upsert(row["id"], row["term_norm"], row["display"], float(row["score"]))
            count += 1
        violations = trie.check_invariants()
        if violations:
            raise IndexDegraded("重建后索引不变量校验失败: " + "; ".join(violations[:5]))
        self.trie = trie
        return count

    def _snapshot_trie(self, version_id: int) -> RadixTrie:
        if not self.storage.snapshot_exists(version_id):
            raise VersionNotFound(
                f"版本 {version_id} 不是已物化快照；历史查询仅支持 baseline 或 snapshot 版本"
            )
        cached = self._snapshot_cache.get(version_id)
        if cached is not None:
            self._snapshot_cache.move_to_end(version_id)
            return cached
        trie = RadixTrie()
        for row in self.storage.iter_snapshot_entries(version_id):
            trie.upsert(row["id"], row["term_norm"], row["display"], float(row["score"]))
        violations = trie.check_invariants()
        if violations:
            raise IndexDegraded(
                f"快照 v{version_id} 索引不变量失败: " + "; ".join(violations[:5])
            )
        self._snapshot_cache[version_id] = trie
        self._snapshot_cache.move_to_end(version_id)
        while len(self._snapshot_cache) > _SNAPSHOT_CACHE_MAX:
            self._snapshot_cache.popitem(last=False)
        return trie

    # ----------------------------------------------------------------- 读

    def _validate_k(self, k: int) -> int:
        if isinstance(k, bool) or not isinstance(k, int):
            raise ValidationFailure("k 必须是整数", field="k")
        if k < 1:
            raise ValidationFailure("k 必须 >= 1", field="k")
        if k > self.topk_max:
            raise ValidationFailure(f"k 不能超过 {self.topk_max}", field="k")
        return k

    def complete(
        self,
        prefix: str,
        k: int = 10,
        version: Optional[int] = None,
        diagnostics: bool = False,
    ) -> CompletionResult:
        """前缀补全（精确 top-k）。

        空前缀 ``""`` 合法：返回整棵词典的 top-k。
        """
        if not isinstance(prefix, str):
            raise ValidationFailure("prefix 必须是字符串", field="prefix")
        k = self._validate_k(k)
        with self._lock:
            if self._degraded:
                raise IndexDegraded("索引处于降级状态，拒绝提供可能不一致的查询")
            prefix_norm = normalize_text(prefix)
            trace = TopKTrace(prefix_norm=prefix_norm, matched=False, node_id=None) \
                if diagnostics else None
            if version is None:
                trie = self.trie
                ver = self.storage.head_version
            else:
                if version == 1:
                    # baseline：空快照的便捷等价
                    trie = RadixTrie()
                    ver = 1
                else:
                    vrow = self.storage.get_version(version)
                    if vrow is None:
                        raise VersionNotFound(f"版本 {version} 不存在")
                    trie = self._snapshot_trie(version)
                    ver = version
            rows = trie.top_k(prefix_norm, k, trace=trace)
            return CompletionResult(rows, prefix, prefix_norm, ver, trace)

    # ----------------------------------------------------------------- 写

    def bulk_upsert(
        self,
        entries: list[dict],
        client_batch_id: Optional[str] = None,
        note: str = "",
    ) -> tuple[int, int, int, str]:
        """批量 upsert；整批原子（任何一条校验失败则整批拒绝）。

        :returns: (version_id, inserted, updated, client_batch_id)
        """
        if not entries:
            raise ValidationFailure("entries 不能为空", field="entries")
        normalized: list[tuple[str, str, str, float]] = []
        seen_ids: set[str] = set()
        for i, e in enumerate(entries):
            term = _validate_term(e.get("term"))
            score = _validate_score(e.get("score"), field=f"entries[{i}].score")
            eid = e.get("id") or f"auto-{new_client_batch_id()[:16]}-{i}"
            if not isinstance(eid, str) or not eid:
                raise ValidationFailure(f"entries[{i}].id 非法", field=f"entries[{i}].id")
            if eid in seen_ids:
                raise ValidationFailure(
                    f"批次内 id 重复: {eid!r}", field=f"entries[{i}].id"
                )
            seen_ids.add(eid)
            term_norm = normalize_text(term)
            if term_norm == "":
                # 原文非空但规范化为空（例如只含会被 NFKC 删除的字符）——拒绝，
                # 否则索引键为空会与空前缀语义混淆。
                raise ValidationFailure(
                    f"entries[{i}].term 规范化后为空，拒绝建立索引",
                    field=f"entries[{i}].term",
                )
            normalized.append((eid, term_norm, term, score))

        batch_id = client_batch_id or new_client_batch_id()
        with self._lock:
            if self._degraded:
                raise IndexDegraded("索引处于降级状态，拒绝写入")
            parent = self.storage.head_version
            # 先在事务内落库并生成新版本
            with self.storage.transaction() as conn:
                version_id = self.storage.new_version(
                    "commit", batch_id, note or "bulk upsert", 0, conn
                )
                inserted = 0
                updated = 0
                for eid, term_norm, display, score in normalized:
                    op = self.storage.apply_upsert(
                        conn, version_id, eid, display, term_norm, score
                    )
                    if op == "inserted":
                        inserted += 1
                    else:
                        updated += 1
                count = self.storage.count_entries()
                self.storage.set_version_count(conn, version_id, count)
                self.storage.set_head(conn, version_id)

            # 存储已提交；在锁内同步更新索引。失败则标记降级并显式报错，
            # 不回退存储（新版本已经是事实），由运维诊断后重建。
            try:
                for eid, term_norm, display, score in normalized:
                    self.trie.upsert(eid, term_norm, display, score)
                violations = self.trie.check_invariants()
                if violations:
                    raise IndexDegraded("写入后不变量失败: " + "; ".join(violations[:5]))
            except IndexDegraded:
                self._degraded = True
                raise
            except Exception as exc:  # pragma: no cover - 防御性
                self._degraded = True
                raise IndexDegraded(f"索引更新失败: {exc}") from exc
            return version_id, inserted, updated, batch_id

    def delete_entry(self, entry_id: str) -> tuple[int, bool]:
        """:returns: (version_id, deleted)"""
        if not isinstance(entry_id, str) or not entry_id:
            raise ValidationFailure("id 必须是非空字符串", field="id")
        with self._lock:
            if self._degraded:
                raise IndexDegraded("索引处于降级状态，拒绝删除")
            existing = self.storage.get_entry(entry_id)
            if existing is None:
                raise EntryNotFound(f"词条 {entry_id!r} 不存在")
            term_norm = existing["term_norm"]
            with self.storage.transaction() as conn:
                version_id = self.storage.new_version(
                    "commit", None, f"delete {entry_id}", 0, conn
                )
                ok = self.storage.apply_delete(conn, version_id, entry_id)
                if not ok:  # 极端并发（理论上锁内不会发生）
                    raise EntryNotFound(f"词条 {entry_id!r} 不存在")
                count = self.storage.count_entries()
                self.storage.set_version_count(conn, version_id, count)
                self.storage.set_head(conn, version_id)
            try:
                removed = self.trie.delete(entry_id, term_norm)
                if not removed:
                    raise IndexDegraded(f"索引中缺少词条 {entry_id!r}")
                violations = self.trie.check_invariants()
                if violations:
                    raise IndexDegraded("删除后不变量失败: " + "; ".join(violations[:5]))
            except IndexDegraded:
                self._degraded = True
                raise
            except Exception as exc:  # pragma: no cover
                self._degraded = True
                raise IndexDegraded(f"索引删除失败: {exc}") from exc
            return version_id, True

    # ----------------------------------------------------------------- 快照

    def snapshot(self, note: str = "") -> dict:
        with self._lock:
            if self._degraded:
                raise IndexDegraded("索引处于降级状态")
            version_id = self.storage.create_snapshot(note)
            # 立即为新快照建立缓存（复用当前 trie 的深拷贝不可得，直接空建后
            # 从持久化行载入，保证缓存来源是数据库而非内存状态）。
            self._snapshot_cache.pop(version_id, None)
            self._snapshot_trie(version_id)
            vrow = self.storage.get_version(version_id)
            return {
                "version": version_id,
                "entry_count": int(vrow["entry_count"]),
                "note": vrow["note"],
                "created_at": vrow["created_at"],
            }

    def restore(self, version: int, note: str = "") -> dict:
        with self._lock:
            if not self.storage.snapshot_exists(version):
                raise VersionNotFound(f"快照版本 {version} 不存在")
            new_id = self.storage.restore_snapshot(version, note)
            try:
                count = self._rebuild_locked()
            except IndexDegraded:
                self._degraded = True
                raise
            # 恢复后旧快照缓存仍有效（快照不可变）
            return {
                "version": new_id,
                "source_snapshot_version": version,
                "entry_count": count,
            }

    # ----------------------------------------------------------------- 诊断

    def status(self) -> dict:
        with self._lock:
            node_count = 0
            terminal_entries = 0
            max_depth = 0
            stack = [(self.trie.root, 0)]
            while stack:
                n, d = stack.pop()
                node_count += 1
                terminal_entries += len(n.terminals)
                if d > max_depth:
                    max_depth = d
                for _, c in n.edges.values():
                    stack.append((c, d + 1))
            violations = self.trie.check_invariants()
            stored = self.storage.count_entries()
            if terminal_entries != stored:
                violations = violations + [
                    f"索引词条数 {terminal_entries} 与存储 {stored} 不一致"
                ]
            return {
                "head_version": self.storage.head_version,
                "normalizer_version": NORMALIZER_VERSION,
                "db_path": self.db_path,
                "degraded": self._degraded,
                "stored_entries": stored,
                "indexed_entries": terminal_entries,
                "node_count": node_count,
                "max_depth": max_depth,
                "healthy": not violations and not self._degraded,
                "violations": violations,
            }

    def check_invariants(self) -> list[str]:
        with self._lock:
            violations = self.trie.check_invariants()
            # 同时校验与存储一致
            counted = sum(len(n.terminals) for n in self.trie.walk_nodes())
            if counted != self.storage.count_entries():
                violations.append(
                    f"索引词条数 {counted} 与存储 {self.storage.count_entries()} 不一致"
                )
            return violations

    def versions(self, limit: int = 100) -> list:
        return [dict(r) for r in self.storage.list_versions(limit)]

    def snapshots(self) -> list:
        return [dict(r) for r in self.storage.list_snapshots()]
