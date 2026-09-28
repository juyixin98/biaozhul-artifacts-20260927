"""三方合并内核。

输入：同一张表在三个快照上的规范化行集 base（共同祖先）/ ours（开发分支）/
theirs（主分支），以及主键定义。
输出：逐条记录键的判定（MergePlan）。

判定原则（对应行为约定）：
1. 用共同祖先区分"独立变化"与"冲突"：以主键识别记录身份，逐字段比较值；
2. 删除对修改必须显式冲突，不允许任何一方静默胜出；
3. 非冲突分区（单边增删改、两边改不同字段、两边都删）直接给出自动结果；
4. 本模块是纯函数，不读文件、不碰数据库，便于独立测试与独立参考实现对照。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from ..domain.models import (
    EntryClass,
    FieldDecision,
    FieldOrigin,
    MergeEntry,
    MergePlan,
    ConflictRecord,
    ResolutionKind,
    TableSpec,
)
from ..errors import ResolutionRejectedError, ValidationError

Rows = list[dict[str, Any]]


# ---------------------------------------------------------------- 工具

def key_of(row: dict[str, Any], primary_key: list[str]) -> tuple[Any, ...]:
    return tuple(row[k] for k in primary_key)


def key_str(key: tuple[Any, ...]) -> str:
    """记录键的稳定字符串标识（日志、存储、测试断言使用）。"""
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))


def index(rows: Iterable[dict[str, Any]], primary_key: list[str]) -> dict[tuple[Any, ...], dict[str, Any]]:
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        out[key_of(row, primary_key)] = row
    return out


def changed_fields(base: dict[str, Any], side: dict[str, Any], columns: list[str]) -> dict[str, bool]:
    return {c: base[c] != side[c] for c in columns}


def _field_decisions(
    columns: list[str],
    base: dict[str, Any] | None,
    ours: dict[str, Any] | None,
    theirs: dict[str, Any] | None,
    origins: dict[str, str],
    merged: dict[str, Any] | None,
) -> dict[str, FieldDecision]:
    def val(side_row: dict[str, Any] | None, c: str) -> Any:
        return None if side_row is None else side_row[c]

    out: dict[str, FieldDecision] = {}
    for c in columns:
        out[c] = FieldDecision(
            origin=origins.get(c, FieldOrigin.BASE.value),
            value=None if merged is None else merged[c],
            ours_value=val(ours, c),
            theirs_value=val(theirs, c),
            base_value=val(base, c),
        )
    return out


# ---------------------------------------------------------------- 主入口

def build_plan(
    spec: TableSpec,
    base_rows: Rows,
    ours_rows: Rows,
    theirs_rows: Rows,
    base_snapshot_id: str,
    ours_snapshot_id: str,
    theirs_snapshot_id: str,
) -> MergePlan:
    columns = [f.name for f in spec.fields]
    base_i = index(base_rows, spec.primary_key)
    ours_i = index(ours_rows, spec.primary_key)
    theirs_i = index(theirs_rows, spec.primary_key)

    all_keys = sorted(set(base_i) | set(ours_i) | set(theirs_i))
    entries: dict[str, MergeEntry] = {}
    conflicts: dict[str, ConflictRecord] = {}

    for k in all_keys:
        b = base_i.get(k)
        o = ours_i.get(k)
        t = theirs_i.get(k)
        ks = key_str(k)
        kdict = dict(zip(spec.primary_key, k))

        if b is not None and o is not None and t is not None:
            entry = _classify_present(kdict, columns, b, o, t)
        elif b is not None and o is None and t is None:
            entry = MergeEntry(
                key=kdict,
                classification=EntryClass.BOTH_DELETED.value,
                conflict=False,
                reason="两边都相对祖先删除了该记录 -> 自动删除",
                merged=None,
                deleted=True,
                base_row=b,
            )
        elif b is not None and o is None:
            entry = _classify_one_deleted(kdict, columns, b, None, t, deleted_side="ours")
        elif b is not None and t is None:
            entry = _classify_one_deleted(kdict, columns, b, o, None, deleted_side="theirs")
        elif b is None and o is not None and t is not None:
            entry = _classify_add_add(kdict, columns, o, t)
        elif b is None and o is not None:
            entry = MergeEntry(
                key=kdict,
                classification=EntryClass.OURS_ADDED.value,
                conflict=False,
                reason="祖先中不存在，仅开发分支新增 -> 自动采用",
                merged=dict(o),
                deleted=False,
                fields=_field_decisions(columns, None, o, None,
                                        {c: FieldOrigin.OURS.value for c in columns}, o),
                ours_row=o,
            )
        else:  # b is None, t is not None, o is None
            entry = MergeEntry(
                key=kdict,
                classification=EntryClass.THEIRS_ADDED.value,
                conflict=False,
                reason="祖先中不存在，仅主分支新增 -> 自动采用",
                merged=dict(t),
                deleted=False,
                fields=_field_decisions(columns, None, None, t,
                                        {c: FieldOrigin.THEIRS.value for c in columns}, t),
                theirs_row=t,
            )

        entries[ks] = entry
        if entry.conflict:
            conflicts[ks] = ConflictRecord(
                key=kdict,
                classification=entry.classification,
                reason=entry.reason,
                base_row=b,
                ours_row=o,
                theirs_row=t,
            )

    return MergePlan(
        table=spec.name,
        primary_key=list(spec.primary_key),
        base_snapshot_id=base_snapshot_id,
        ours_snapshot_id=ours_snapshot_id,
        theirs_snapshot_id=theirs_snapshot_id,
        entries=entries,
        conflicts=conflicts,
    )


def _classify_present(
    kdict: dict[str, Any],
    columns: list[str],
    b: dict[str, Any],
    o: dict[str, Any],
    t: dict[str, Any],
) -> MergeEntry:
    o_changed = {c: b[c] != o[c] for c in columns}
    t_changed = {c: b[c] != t[c] for c in columns}
    o_differs = any(o_changed.values())
    t_differs = any(t_changed.values())

    if not o_differs and not t_differs:
        return MergeEntry(
            key=kdict,
            classification=EntryClass.UNCHANGED.value,
            conflict=False,
            reason="两边都与祖先相同 -> 保持不变",
            merged=dict(b),
            deleted=False,
            fields=_field_decisions(columns, b, o, t,
                                    {c: FieldOrigin.BASE.value for c in columns}, b),
            base_row=b, ours_row=o, theirs_row=t,
        )

    if o_differs and not t_differs:
        return MergeEntry(
            key=kdict,
            classification=EntryClass.OURS_MODIFIED.value,
            conflict=False,
            reason="仅开发分支相对祖先修改 -> 自动采用开发分支版本",
            merged=dict(o),
            deleted=False,
            fields=_fields_take_side(columns, b, o, t, FieldOrigin.OURS, o, o_changed),
            base_row=b, ours_row=o, theirs_row=t,
        )

    if t_differs and not o_differs:
        return MergeEntry(
            key=kdict,
            classification=EntryClass.THEIRS_MODIFIED.value,
            conflict=False,
            reason="仅主分支相对祖先修改 -> 自动采用主分支版本",
            merged=dict(t),
            deleted=False,
            fields=_fields_take_side(columns, b, o, t, FieldOrigin.THEIRS, t, t_changed),
            base_row=b, ours_row=o, theirs_row=t,
        )

    # 两边都相对祖先修改
    if o == t:
        merged = dict(o)
        origins = {c: FieldOrigin.AGREED.value if o_changed[c] or t_changed[c]
                   else FieldOrigin.BASE.value for c in columns}
        return MergeEntry(
            key=kdict,
            classification=EntryClass.FIELD_MERGE.value,
            conflict=False,
            reason="两边对记录做了完全相同的修改 -> 自动采用一致结果",
            merged=merged,
            deleted=False,
            fields=_field_decisions(columns, b, o, t, origins, merged),
            base_row=b, ours_row=o, theirs_row=t,
        )

    both_changed = [c for c in columns if o_changed[c] and t_changed[c]]
    clashing = [c for c in both_changed if o[c] != t[c]]
    if not clashing:
        # 两边改的是不同字段（或改到相同值）-> 字段级自动合并
        merged = dict(b)
        origins: dict[str, str] = {}
        for c in columns:
            if o_changed[c]:
                merged[c] = o[c]
                origins[c] = (FieldOrigin.AGREED.value if t_changed[c]
                              else FieldOrigin.OURS.value)
            elif t_changed[c]:
                merged[c] = t[c]
                origins[c] = FieldOrigin.THEIRS.value
            else:
                origins[c] = FieldOrigin.BASE.value
        dev_fields = [c for c in columns if o_changed[c] and not t_changed[c]]
        main_fields = [c for c in columns if t_changed[c] and not o_changed[c]]
        agreed = [c for c in both_changed if o[c] == t[c]]
        return MergeEntry(
            key=kdict,
            classification=EntryClass.FIELD_MERGE.value,
            conflict=False,
            reason=(
                f"两边修改互不相交的字段 -> 字段级自动合并；"
                f"开发分支字段={dev_fields}，主分支字段={main_fields}，同值字段={agreed}"
            ),
            merged=merged,
            deleted=False,
            fields=_field_decisions(columns, b, o, t, origins, merged),
            base_row=b, ours_row=o, theirs_row=t,
        )

    # 同一字段被两边改成不同值 -> 冲突；不冲突字段先自动并入
    merged = dict(b)
    origins = {}
    for c in columns:
        if c in clashing:
            origins[c] = FieldOrigin.CONFLICT.value  # 暂存祖先值
        elif o_changed[c]:
            merged[c] = o[c]
            origins[c] = FieldOrigin.OURS.value
        elif t_changed[c]:
            merged[c] = t[c]
            origins[c] = FieldOrigin.THEIRS.value
        else:
            origins[c] = FieldOrigin.BASE.value
    detail = "; ".join(
        f"{c}: 祖先={b[c]!r} 开发={o[c]!r} 主={t[c]!r}" for c in clashing
    )
    return MergeEntry(
        key=kdict,
        classification=EntryClass.FIELD_VALUE_CONFLICT.value,
        conflict=True,
        reason=f"同一字段被两边改成不同值 -> 冲突字段 {clashing}（{detail}）",
        merged=merged,
        deleted=False,
        fields=_field_decisions(columns, b, o, t, origins, merged),
        base_row=b, ours_row=o, theirs_row=t,
        conflicting_fields=list(clashing),
    )


def _fields_take_side(
    columns: list[str],
    b: dict[str, Any],
    o: dict[str, Any],
    t: dict[str, Any],
    side: FieldOrigin,
    merged: dict[str, Any],
    changed: dict[str, bool],
) -> dict[str, FieldDecision]:
    origins = {c: (side.value if changed[c] else FieldOrigin.BASE.value) for c in columns}
    return _field_decisions(columns, b, o, t, origins, merged)


def _classify_one_deleted(
    kdict: dict[str, Any],
    columns: list[str],
    b: dict[str, Any],
    o: dict[str, Any] | None,
    t: dict[str, Any] | None,
    deleted_side: str,
) -> MergeEntry:
    surviving = t if deleted_side == "ours" else o
    survivor_name = "主分支" if deleted_side == "ours" else "开发分支"
    deleter_name = "开发分支" if deleted_side == "ours" else "主分支"

    assert surviving is not None
    if surviving == b:
        # 删除方删除、另一方未改 -> 自动删除
        cls = EntryClass.OURS_DELETED if deleted_side == "ours" else EntryClass.THEIRS_DELETED
        return MergeEntry(
            key=kdict,
            classification=cls.value,
            conflict=False,
            reason=f"{deleter_name}删除而{survivor_name}未修改 -> 自动删除",
            merged=None,
            deleted=True,
            base_row=b, ours_row=o, theirs_row=t,
        )

    # 删除对修改：明确保留为冲突，任何一方都不得静默胜出
    changed = [c for c in columns if surviving[c] != b[c]]
    return MergeEntry(
        key=kdict,
        classification=EntryClass.DELETE_MODIFY_CONFLICT.value,
        conflict=True,
        reason=(
            f"{deleter_name}删除了该记录，而{survivor_name}修改了字段 {changed} -> "
            "删除/修改冲突，必须显式解决"
        ),
        merged=None,
        deleted=False,
        fields=_field_decisions(columns, b, o, t, {}, None),
        base_row=b, ours_row=o, theirs_row=t,
    )


def _classify_add_add(
    kdict: dict[str, Any],
    columns: list[str],
    o: dict[str, Any],
    t: dict[str, Any],
) -> MergeEntry:
    if o == t:
        origins = {c: FieldOrigin.AGREED.value for c in columns}
        return MergeEntry(
            key=kdict,
            classification=EntryClass.ADD_ADD_IDENTICAL.value,
            conflict=False,
            reason="两边独立新增了完全相同的记录 -> 自动采用",
            merged=dict(o),
            deleted=False,
            fields=_field_decisions(columns, None, o, t, origins, o),
            ours_row=o, theirs_row=t,
        )
    diff_cols = [c for c in columns if o[c] != t[c]]
    return MergeEntry(
        key=kdict,
        classification=EntryClass.ADD_ADD_CONFLICT.value,
        conflict=True,
        reason=f"两边用相同主键独立新增但字段值不同 -> 冲突字段 {diff_cols}",
        merged=None,
        deleted=False,
        fields=_field_decisions(columns, None, o, t, {}, None),
        ours_row=o, theirs_row=t,
        conflicting_fields=diff_cols,
    )


# ---------------------------------------------------------------- 冲突解决（纯判定）

def apply_resolution(
    entry: MergeEntry,
    primary_key: list[str],
    kind: str,
    custom_row: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bool]:
    """把一个解决方案应用到冲突条目，返回 (最终行或None, 是否删除)。

    仅做结构性判定（该解决方案是否适用于此冲突类别、字段是否齐全）；
    值的类型校验在服务层用 Schema 完成。
    """
    cls = entry.classification
    valid = {r.value for r in ResolutionKind}
    if kind not in valid:
        raise ResolutionRejectedError(f"未知解决方案 {kind!r}，可选: {sorted(valid)}")

    if kind == ResolutionKind.OURS.value:
        return (None, True) if entry.ours_row is None else (dict(entry.ours_row), False)
    if kind == ResolutionKind.THEIRS.value:
        return (None, True) if entry.theirs_row is None else (dict(entry.theirs_row), False)
    if kind == ResolutionKind.DELETE.value:
        return None, True

    if kind == ResolutionKind.KEEP.value:
        # 仅在删除/修改冲突中有意义：保留未删除一侧的修改后版本
        if cls != EntryClass.DELETE_MODIFY_CONFLICT.value:
            raise ResolutionRejectedError("KEEP 仅适用于删除/修改冲突")
        survivor = entry.theirs_row if entry.ours_row is None else entry.ours_row
        if survivor is None:  # pragma: no cover - 不可能
            raise ResolutionRejectedError("没有可保留的一侧")
        return dict(survivor), False

    # kind == VALUE
    if cls == EntryClass.FIELD_VALUE_CONFLICT.value:
        if not custom_row:
            raise ResolutionRejectedError("VALUE 解决取值冲突时必须提供字段值")
        merged = dict(entry.merged)  # 非冲突字段已自动并入
        extra = [c for c in custom_row if c not in merged]
        if extra:
            raise ResolutionRejectedError(f"提供了未知字段: {extra}")
        only_conflict = [
            c for c in custom_row if c not in entry.conflicting_fields
        ]
        if only_conflict:
            raise ResolutionRejectedError(
                f"非冲突字段已自动合并，VALUE 只允许覆盖冲突字段；越界字段: {only_conflict}"
            )
        missing = [c for c in entry.conflicting_fields if c not in custom_row]
        if missing:
            raise ResolutionRejectedError(
                f"VALUE 必须覆盖全部冲突字段，缺少: {missing}"
            )
        merged.update(custom_row)
        return merged, False

    if cls == EntryClass.ADD_ADD_CONFLICT.value:
        if not custom_row:
            raise ResolutionRejectedError("VALUE 解决同键新增冲突时必须提供完整行")
        template = entry.ours_row or entry.theirs_row or {}
        final = dict(custom_row)
        # 主键身份不可被解决方案改变
        for pk in primary_key:
            final[pk] = entry.key[pk]
        unknown = [c for c in final if c not in template]
        if unknown:
            raise ResolutionRejectedError(f"提供了未知字段: {unknown}")
        missing = [c for c in template if c not in final]
        if missing:
            raise ResolutionRejectedError(
                f"VALUE 解决同键新增冲突必须提供完整行，缺少: {missing}"
            )
        return final, False

    if cls == EntryClass.DELETE_MODIFY_CONFLICT.value:
        if not custom_row:
            raise ResolutionRejectedError("VALUE 解决删除/修改冲突时必须提供完整行")
        final = dict(custom_row)
        for pk in primary_key:
            final[pk] = entry.key[pk]
        template = entry.theirs_row or entry.ours_row or {}
        unknown = [c for c in final if c not in template]
        if unknown:
            raise ResolutionRejectedError(f"提供了未知字段: {unknown}")
        missing = [c for c in template if c not in final]
        if missing:
            raise ResolutionRejectedError(f"VALUE 缺少字段: {missing}")
        return final, False

    raise ResolutionRejectedError(f"冲突类别 {cls} 不接受 VALUE 解决方案")  # pragma: no cover


def assert_same_schema(a: TableSpec, b: TableSpec, context: str) -> None:
    """三方快照必须结构兼容才允许合并。"""
    if a.to_dict()["fields"] != b.to_dict()["fields"] or a.primary_key != b.primary_key:
        raise ValidationError(
            f"{context}: 三方快照 Schema 不一致，无法按同一记录身份合并 "
            f"(主键 {a.primary_key} vs {b.primary_key})"
        )
