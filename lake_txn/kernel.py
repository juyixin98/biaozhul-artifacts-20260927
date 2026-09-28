"""执行内核：纯逻辑，不做任何 I/O。

把“并发提交如何裁决”“新快照的文件清单如何构造”从数据库/文件系统中
剥离出来，使其可以脱离被测服务独立、穷举地测试。

裁决规则（本服务是 Iceberg 乐观并发的简化子集，见 README“非目标”）：
- 基线必须存在且不新于当前表头快照。
- 基线之后存在其它提交（committed）时：
  * APPEND：仅当与所有并发提交触及的分区互不相交时才允许自动合并；
    分区相交 -> PARTITION_CONFLICT；与并发 OVERWRITE 相交 -> CONCURRENT_OVERWRITE。
  * OVERWRITE：存在任何并发提交时一律不自动重放：
    - 并发含 OVERWRITE            -> CONCURRENT_OVERWRITE
    - 并发 APPEND 与本次范围相交  -> PARTITION_CONFLICT
    - 并发 APPEND 与本次范围不相交 -> STALE_BASE_OVERWRITE（要求刷新后重试）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lake_txn import errors


class CommitKind(str, Enum):
    APPEND = "APPEND"
    OVERWRITE = "OVERWRITE"


class Outcome(str, Enum):
    ACCEPTED = "ACCEPTED"            # 基线即表头，直接接受
    ACCEPTED_MERGE = "ACCEPTED_MERGE"  # 陈旧基线，按声明规则合并后接受
    REJECTED = "REJECTED"            # 确定性拒绝


@dataclass(frozen=True)
class CompetingCommit:
    """基线之后、已成功落库的其它提交。"""

    request_id: str
    kind: CommitKind
    # APPEND：新增文件所在分区；OVERWRITE：被替换(drop)与新增(add)分区并集
    partitions: frozenset[str]


@dataclass(frozen=True)
class CommitIntent:
    table: str
    request_id: str
    kind: CommitKind
    base_snapshot_id: int
    # 本次新增文件所在分区
    add_partitions: frozenset[str]
    # OVERWRITE 时声明要替换的分区；APPEND 为空
    drop_partitions: frozenset[str] = frozenset()

    @property
    def scope(self) -> frozenset[str]:
        """本次提交对数据可能产生影响的全部分区。"""
        return self.add_partitions | self.drop_partitions


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason_code: str | None
    conflict_partitions: frozenset[str] = frozenset()
    concurrent_request_ids: tuple[str, ...] = ()
    merged: bool = False

    @property
    def accepted(self) -> bool:
        return self.outcome in (Outcome.ACCEPTED, Outcome.ACCEPTED_MERGE)


def adjudicate(
    intent: CommitIntent,
    head_snapshot_id: int,
    concurrent: tuple[CompetingCommit, ...],
) -> Decision:
    """对一次提交做纯逻辑裁决。concurrent 为基线之后的其它已提交记录。"""
    if intent.base_snapshot_id > head_snapshot_id:
        return Decision(
            Outcome.REJECTED,
            errors.BASE_IN_FUTURE,
            concurrent_request_ids=tuple(c.request_id for c in concurrent),
        )

    if not concurrent:
        return Decision(Outcome.ACCEPTED, None)

    others = tuple(c.request_id for c in concurrent)
    concurrent_overwrites = tuple(c for c in concurrent if c.kind == CommitKind.OVERWRITE)
    concurrent_appends = tuple(c for c in concurrent if c.kind == CommitKind.APPEND)

    if intent.kind is CommitKind.OVERWRITE:
        if concurrent_overwrites:
            conflict = intent.scope & _union(concurrent_overwrites)
            return Decision(
                Outcome.REJECTED,
                errors.CONCURRENT_OVERWRITE,
                conflict_partitions=frozenset(conflict),
                concurrent_request_ids=others,
            )
        # 并发只有 APPEND
        appended = _union(concurrent_appends)
        conflict = intent.scope & appended
        if conflict:
            return Decision(
                Outcome.REJECTED,
                errors.PARTITION_CONFLICT,
                conflict_partitions=frozenset(conflict),
                concurrent_request_ids=others,
            )
        return Decision(
            Outcome.REJECTED,
            errors.STALE_BASE_OVERWRITE,
            concurrent_request_ids=others,
        )

    # APPEND
    if concurrent_overwrites:
        ow_parts = _union(concurrent_overwrites)
        conflict = intent.add_partitions & ow_parts
        if conflict:
            return Decision(
                Outcome.REJECTED,
                errors.CONCURRENT_OVERWRITE,
                conflict_partitions=frozenset(conflict),
                concurrent_request_ids=others,
            )
        # 并发 OVERWRITE 不触及本提交分区：以新表头为父快照合并追加
        appended = _union(concurrent_appends)
        if intent.add_partitions & appended:
            return Decision(
                Outcome.REJECTED,
                errors.PARTITION_CONFLICT,
                conflict_partitions=frozenset(intent.add_partitions & appended),
                concurrent_request_ids=others,
            )
        return Decision(
            Outcome.ACCEPTED_MERGE,
            None,
            concurrent_request_ids=others,
            merged=True,
        )

    # 并发只有 APPEND
    appended = _union(concurrent_appends)
    conflict = intent.add_partitions & appended
    if conflict:
        return Decision(
            Outcome.REJECTED,
            errors.PARTITION_CONFLICT,
            conflict_partitions=frozenset(conflict),
            concurrent_request_ids=others,
        )
    return Decision(
        Outcome.ACCEPTED_MERGE,
        None,
        concurrent_request_ids=others,
        merged=True,
    )


@dataclass(frozen=True)
class ManifestFile:
    """清单中的一个数据文件条目（不可变 Parquet）。"""

    path: str  # 相对仓库根的数据文件路径
    partition: str
    sha256: str
    size_bytes: int
    row_count: int


def plan_manifest(
    kind: CommitKind,
    head_files: tuple[ManifestFile, ...],
    new_files: tuple[ManifestFile, ...],
    drop_partitions: frozenset[str],
) -> tuple[ManifestFile, ...]:
    """以“裁决后选定的父快照清单”为基础构造新清单（纯函数）。

    - APPEND：保留父清单全部文件，并入新文件（按路径去重，支持重试合并）。
    - OVERWRITE：剔除被替换分区的旧文件，保留其它分区，再并入新文件。
    """
    if kind is CommitKind.APPEND:
        merged: dict[str, ManifestFile] = {f.path: f for f in head_files}
        for f in new_files:
            merged.setdefault(f.path, f)
        return tuple(merged.values())

    kept = tuple(f for f in head_files if f.partition not in drop_partitions)
    merged = {f.path: f for f in kept}
    for f in new_files:
        merged.setdefault(f.path, f)
    return tuple(merged.values())


def partitions_of_rows(
    rows: list[dict], partition_column: str
) -> tuple[str, ...]:
    """从记录中抽取分区值并转成字符串分区标识；空值/缺失非法。"""
    values = {str(r[partition_column]) for r in rows}
    return tuple(sorted(values))


def _union(commits: tuple[CompetingCommit, ...]) -> frozenset[str]:
    out: set[str] = set()
    for c in commits:
        out |= set(c.partitions)
    return frozenset(out)
