"""编排层：规范化校验 + 压缩 Trie 索引 + SQLite 存储。

职责：
- 入参校验（显示原文、词频、k），非法输入抛具体错误码，绝不返回成功；
- 所有写操作“先落盘（事务）再改内存索引”，保证快照与索引一致；
- 热词降权/删除后索引上界立即重算，``verify`` 可独立核对；
- 快照恢复后从落盘数据完整重建内存 trie。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .errors import (
    DuplicateId,
    EntryNotFound,
    IndexCorrupt,
    InvalidLimit,
    InvalidScore,
    InvalidSurface,
)
from .normalizer import NORMALIZER_VERSION, normalize
from .oracle import oracle_subtree_max, oracle_top_k
from .store import SqliteStore
from .trie import CompressedTrie, Entry, TopKTrace


@dataclass(frozen=True, slots=True)
class CompletionResult:
    prefix: str
    normalized_prefix: str
    k: int
    entries: list[Entry]
    trace: TopKTrace


class Engine:
    def __init__(self, db_path: Path, snapshot_dir: Path, *, max_surface_len: int = 512,
                 max_limit: int = 1000, max_batch: int = 10000) -> None:
        self.max_surface_len = max_surface_len
        self.max_limit = max_limit
        self.max_batch = max_batch
        self.store = SqliteStore(db_path, snapshot_dir)
        self.trie = CompressedTrie()
        self._build_index()

    # ---- 初始化/重建 -----------------------------------------------------

    def _build_index(self) -> int:
        self.trie = CompressedTrie()
        entries = self.store.load_all()
        for e in entries:
            self.trie.upsert(e)
        return len(entries)

    def versions(self) -> dict[str, str]:
        return {
            "normalizer_version": NORMALIZER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "revision": str(self.store.revision()),
        }

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self.trie),
            "trie_nodes": self.trie.count_nodes(),
            "revision": self.store.revision(),
        }

    # ---- 校验 ------------------------------------------------------------

    def _validate_surface(self, surface: object) -> str:
        if not isinstance(surface, str) or surface == "":
            raise InvalidSurface(
                "显示原文必须是非空字符串", details={"received_type": type(surface).__name__}
            )
        if len(surface) > self.max_surface_len:
            raise InvalidSurface(
                f"显示原文长度超过上限 {self.max_surface_len}",
                details={"length": len(surface), "max": self.max_surface_len},
            )
        if "\x00" in surface:
            raise InvalidSurface(
                "显示原文不得包含 NUL 字符", details={"contains_nul": True}
            )
        if normalize(surface) == "":
            raise InvalidSurface(
                "规范化后为空（如纯空白）的字符串不能作为词条",
                details={"surface": surface},
            )
        return surface

    def _validate_score(self, score: object) -> int:
        # bool 是 int 的子类，但语义上不是词频，显式拒绝。
        if isinstance(score, bool) or not isinstance(score, int):
            raise InvalidScore(
                "词频必须是非负整数", details={"received_type": type(score).__name__}
            )
        if score < 0:
            raise InvalidScore(
                "词频不能为负数", details={"score": score}
            )
        return score

    def _validate_k(self, k: object) -> int:
        if isinstance(k, bool) or not isinstance(k, int):
            raise InvalidLimit(
                "k 必须是正整数", details={"received_type": type(k).__name__}
            )
        if k < 1 or k > self.max_limit:
            raise InvalidLimit(
                f"k 必须在 1..{self.max_limit} 之间", details={"k": k, "max": self.max_limit}
            )
        return k

    def _make_entry(self, entry_id: str, surface: str, score: int) -> Entry:
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise InvalidSurface("词条 id 必须是非空字符串", details={"id": entry_id})
        return Entry(
            id=entry_id,
            surface=surface,
            key=normalize(surface),
            score=score,
        )

    # ---- 写操作 ----------------------------------------------------------

    def upsert(self, entry_id: str, surface: str, score: int) -> Entry:
        surface = self._validate_surface(surface)
        score = self._validate_score(score)
        entry = self._make_entry(entry_id, surface, score)
        # 先落盘（事务内），再更新内存；落盘失败则索引完全不动。
        self.store.upsert_many([entry])
        self.trie.upsert(entry)
        return entry

    def upsert_batch(
        self, items: list[dict[str, object]]
    ) -> dict[str, Any]:
        if not isinstance(items, list) or not items:
            raise InvalidSurface("批量写入必须是非空列表")
        if len(items) > self.max_batch:
            raise InvalidLimit(
                f"批量条目数超过上限 {self.max_batch}",
                details={"size": len(items), "max": self.max_batch},
            )
        entries: list[Entry] = []
        seen: dict[str, str] = {}
        # 先全量校验：任何一条非法，整批不写入（原子语义）。
        for item in items:
            if not isinstance(item, dict):
                raise InvalidSurface("批量条目必须是对象", details={"item": str(item)})
            eid = item.get("id")
            if not isinstance(eid, str) or not eid.strip():
                raise InvalidSurface("条目缺少非空 id", details={"item": item})
            surface = self._validate_surface(item.get("surface"))
            score = self._validate_score(item.get("score"))
            entry = self._make_entry(eid, surface, score)
            if eid in seen and seen[eid] != entry.surface:
                raise DuplicateId(
                    f"批量中 id {eid!r} 对应了不同显示原文",
                    details={"id": eid, "surfaces": [seen[eid], entry.surface]},
                )
            seen[eid] = entry.surface
            entries.append(entry)
        self.store.upsert_many(entries)
        for entry in entries:
            self.trie.upsert(entry)
        return {"written": len(entries), "revision": self.store.revision()}

    def set_score(self, entry_id: str, score: int) -> Entry:
        score = self._validate_score(score)
        current = self.store.get(entry_id)
        if current is None:
            raise EntryNotFound(f"词条 {entry_id!r} 不存在", details={"id": entry_id})
        updated = Entry(current.id, current.surface, current.key, score)
        self.store.set_score(entry_id, score)
        self.trie.upsert(updated)
        return updated

    def adjust_score(self, entry_id: str, delta: int) -> Entry:
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise InvalidScore(
                "增量必须是整数（可为负）", details={"received_type": type(delta).__name__}
            )
        current = self.store.get(entry_id)
        if current is None:
            raise EntryNotFound(f"词条 {entry_id!r} 不存在", details={"id": entry_id})
        new_score = current.score + delta
        if new_score < 0:
            raise InvalidScore(
                "增减后词频不能为负",
                details={"current": current.score, "delta": delta, "result": new_score},
            )
        return self.set_score(entry_id, new_score)

    def delete(self, entry_id: str) -> None:
        current = self.store.get(entry_id)
        if current is None:
            raise EntryNotFound(f"词条 {entry_id!r} 不存在", details={"id": entry_id})
        self.store.delete(entry_id)
        self.trie.delete(entry_id)

    # ---- 查询 ------------------------------------------------------------

    def complete(
        self, raw_prefix: str, k: int, *, trace: bool = False
    ) -> CompletionResult:
        if not isinstance(raw_prefix, str):
            raise InvalidSurface("前缀必须是字符串")
        k = self._validate_k(k)
        pfx = normalize(raw_prefix)
        entries, tr = self.trie.top_k(pfx, k, collect_trace=trace)
        tr.prefix = raw_prefix
        tr.normalized_prefix = pfx
        return CompletionResult(
            prefix=raw_prefix, normalized_prefix=pfx, k=k, entries=entries, trace=tr
        )

    # ---- 诊断 ------------------------------------------------------------

    def verify(self, *, deep: bool = False, sample_prefixes: list[str] | None = None) -> dict[str, Any]:
        """结构/上界完整性校验；deep=True 时再与独立 oracle 全量对拍。

        任何不一致都如实返回 ``ok=False`` 与具体违规，不统一报成功。
        """
        violations = self.trie.verify_integrity()
        result: dict[str, Any] = {
            "ok": not violations,
            "integrity_violations": violations,
            "cross_check": None,
        }
        if violations:
            raise IndexCorrupt(
                f"索引完整性校验发现 {len(violations)} 处违规（剪枝上界/结构不变量不可信）",
                details={"violations": violations[:20], "total": len(violations)},
            )
        if deep:
            all_entries = self.store.load_all()
            # 默认用一组有代表性的前缀；调用方也可显式指定。
            prefixes = sample_prefixes
            if prefixes is None:
                prefixes = [""]
                # 加入每个词条的 1/2/3 字符前缀，覆盖面足够而又不爆炸。
                sampled = sorted({e.key[:n] for e in all_entries for n in (1, 2, 3)})
                prefixes.extend(sampled[:200])
            mismatches: list[dict[str, Any]] = []
            prune_checks: list[dict[str, Any]] = []
            for p in prefixes:
                np = normalize(p)
                got, tr = self.trie.top_k(np, 5, collect_trace=True)
                exp = oracle_top_k(all_entries, p, 5).entries
                if [e.id for e in got] != [e.id for e in exp]:
                    mismatches.append(
                        {
                            "prefix": p,
                            "normalized_prefix": np,
                            "trie_ids": [e.id for e in got],
                            "oracle_ids": [e.id for e in exp],
                        }
                    )
                # 上界依据核对：每次剪枝记录的上界必须等于暴力子树最大值，
                # 且严格小于当时的第 k 名分数。
                for sub_pfx, reason in tr.prunes:
                    true_max = oracle_subtree_max(all_entries, sub_pfx)
                    prune_checks.append(
                        {
                            "subtree_prefix": sub_pfx,
                            "claimed_upper_bound": reason.upper_bound,
                            "true_subtree_max": true_max,
                            "best_k_score": reason.best_k_score,
                            "bound_is_valid": (
                                true_max is not None
                                and reason.upper_bound == true_max
                                and reason.upper_bound < reason.best_k_score
                            ),
                        }
                    )
            invalid_bounds = [c for c in prune_checks if not c["bound_is_valid"]]
            result["cross_check"] = {
                "prefixes_checked": len(prefixes),
                "mismatches": mismatches,
                "prune_checks": prune_checks[:200],
                "prune_checks_total": len(prune_checks),
                "invalid_bounds": invalid_bounds,
                "ok": not mismatches and not invalid_bounds,
            }
            result["ok"] = not mismatches and not invalid_bounds
            if mismatches or invalid_bounds:
                raise IndexCorrupt(
                    "深度对拍发现 trie 结果或剪枝上界与独立 oracle 不一致",
                    details={
                        "mismatches": mismatches[:10],
                        "invalid_bounds": invalid_bounds[:10],
                    },
                )
        return result

    # ---- 快照 ------------------------------------------------------------

    def create_snapshot(self, name: str, note: str = "") -> dict[str, Any]:
        return self.store.create_snapshot(name, note)

    def list_snapshots(self) -> list[dict[str, Any]]:
        return self.store.list_snapshots()

    def restore_snapshot(self, name: str) -> dict[str, Any]:
        record = self.store.restore_snapshot(name)
        rebuilt = self._build_index()
        # 恢复后立即自检：上界不可信时直接报损坏，而不是带着坏索引继续服务。
        violations = self.trie.verify_integrity()
        if violations:
            raise IndexCorrupt(
                "快照恢复后重建索引未通过完整性校验",
                details={"violations": violations[:20]},
            )
        return {"restored": record, "rebuilt_entries": rebuilt}

    def close(self) -> None:
        self.store.close()
