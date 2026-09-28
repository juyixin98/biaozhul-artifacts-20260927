"""合并内核单元测试：逐场景断言具体行集、字段值与冲突分类。

期望值是测试内手写的字面量（独立参考答案），不是调用被测内核生成的。
另见 tests/oracle.py 的独立实现及 test_fuzz_against_oracle 的随机交叉验证。
"""
from __future__ import annotations

import pytest

from table_merge.errors import InvalidResolutionError, SchemaMismatchError
from table_merge.merge_kernel import (
    apply_resolutions,
    ensure_compatible_schemas,
    three_way_merge,
    validate_resolution,
)
from table_merge.models import (
    Column,
    ResolutionAction,
    RowDecision,
    Snapshot,
    TableSchema,
    key_string,
)

from .conftest import EMP_COLUMNS, EMP_SCHEMA, row
from .oracle import (
    ADD_ADD_CONFLICT,
    DELETE_MODIFY_CONFLICT,
    FIELD_MERGE,
    FAST_FORWARD,
    SAME_FIELD_CONFLICT,
    UNCHANGED,
    oracle_merge,
)

SCHEMA = TableSchema.from_dict(EMP_SCHEMA)


def _snap(sid: str) -> Snapshot:
    return Snapshot(snapshot_id=sid, table="employees", schema=SCHEMA,
                    parquet_path=f"{sid}.parquet", row_count=0, content_hash=sid)


def _merge(base, dev, main):
    return three_way_merge(SCHEMA, base, dev, main,
                           base_snapshot_id="B", dev_snapshot_id="D", main_snapshot_id="M")


def _auto_map(report):
    return {d.key[0]: d for d in report.decisions}


# ---------------------------------------------------------------------------
# 1) 相同修改：双方都未动 -> UNCHANGED；双方改成相同值 -> 收敛
# ---------------------------------------------------------------------------

def test_identical_state_is_unchanged():
    base = [row(1, "a", "x", 1)]
    report = _merge(base, [row(1, "a", "x", 1)], [row(1, "a", "x", 1)])
    d = _auto_map(report)[1]
    assert d.decision is RowDecision.UNCHANGED
    assert report.merged_rows == [row(1, "a", "x", 1)]
    assert report.conflicts == []


def test_both_sides_make_identical_edit_converges():
    base = [row(1, "a", "x", 1)]
    edited = [row(1, "a", "y", 1)]
    report = _merge(base, edited, edited)
    d = _auto_map(report)[1]
    assert d.decision is RowDecision.FIELD_MERGE
    assert report.merged_rows == edited  # 同值收敛，不是冲突


# ---------------------------------------------------------------------------
# 2) 互斥新增：各加不同主键 -> 都保留
# ---------------------------------------------------------------------------

def test_disjoint_inserts_both_kept():
    dev = [row(7, "g", "s", 7), row(1, "a", "x", 1)]
    main = [row(8, "h", "t", 8), row(1, "a", "x", 1)]
    report = _merge([row(1, "a", "x", 1)], dev, main)
    by_key = _auto_map(report)
    assert by_key[7].decision is RowDecision.FAST_FORWARD
    assert by_key[8].decision is RowDecision.FAST_FORWARD
    assert {r["id"] for r in report.merged_rows} == {1, 7, 8}


# ---------------------------------------------------------------------------
# 3) 同键不同字段：不相交字段编辑 -> FIELD_MERGE，逐字段取变化侧
# ---------------------------------------------------------------------------

def test_disjoint_field_edits_merge_field_by_field():
    base = [row(2, "b", "sh", 20)]
    dev = [row(2, "b", "bj", 25)]              # 改 city, score
    main = [row(2, "b", "sh", 20, active=False)]  # 只改 active
    report = _merge(base, dev, main)
    d = _auto_map(report)[2]
    assert d.decision is RowDecision.FIELD_MERGE
    assert set(d.changed_fields_dev) == {"city", "score"}
    assert d.changed_fields_main == ("active",)
    assert report.merged_rows == [row(2, "b", "bj", 25, active=False)]


# ---------------------------------------------------------------------------
# 4) 同键同字段不同值 -> SAME_FIELD_CONFLICT；重叠字段同值仍可合并
# ---------------------------------------------------------------------------

def test_same_field_different_values_conflicts():
    base = [row(6, "f", "wh", 60, active=True)]
    dev = [row(6, "f", "wh", 66, active=False)]
    main = [row(6, "f", "wh", 99, active=False)]
    report = _merge(base, dev, main)
    d = _auto_map(report)[6]
    assert d.decision is RowDecision.SAME_FIELD_CONFLICT
    # active 两侧都改成 false（收敛），只有 score 冲突
    assert d.changed_fields_dev == ("score", "active")
    assert report.merged_rows == []  # 冲突行绝不进自动合并集


def test_overlapping_field_with_same_value_plus_disjoint_edit_merges():
    base = [row(6, "f", "wh", 60, active=True)]
    dev = [row(6, "f", "wh", 66, active=False)]
    main = [row(6, "f", "gz", 99, active=False)]
    report = _merge(base, dev, main)
    d = _auto_map(report)[6]
    # score 两侧值不同 -> 仍是同字段冲突，尽管 active 收敛
    assert d.decision is RowDecision.SAME_FIELD_CONFLICT


# ---------------------------------------------------------------------------
# 5) 删除/修改冲突（两个方向）明确保留；一侧删另一侧不动 -> 快进删除
# ---------------------------------------------------------------------------

def test_dev_deletes_main_modifies_conflicts():
    base = [row(4, "d", "hz", 40)]
    report = _merge(base, [], [row(4, "d", "hf", 41)])
    d = _auto_map(report)[4]
    assert d.decision is RowDecision.DELETE_MODIFY_CONFLICT
    assert d.dev_row is None and d.main_row == row(4, "d", "hf", 41)
    assert report.merged_rows == []
    assert "deleted" in d.basis and "main" in d.basis


def test_main_deletes_dev_modifies_conflicts():
    base = [row(4, "d", "hz", 40)]
    report = _merge(base, [row(4, "d", "bj", 42)], [])
    d = _auto_map(report)[4]
    assert d.decision is RowDecision.DELETE_MODIFY_CONFLICT
    assert d.main_row is None and d.dev_row == row(4, "d", "bj", 42)


def test_one_side_deletes_other_untouched_fast_forwards_to_deletion():
    base = [row(3, "c", "sz", 30)]
    report = _merge(base, [], [row(3, "c", "sz", 30)])
    d = _auto_map(report)[3]
    assert d.decision is RowDecision.FAST_FORWARD
    assert report.merged_rows == []  # 删除快进，行消失


def test_both_sides_delete_fast_forwards():
    base = [row(3, "c", "sz", 30)]
    report = _merge(base, [], [])
    assert _auto_map(report)[3].decision is RowDecision.FAST_FORWARD
    assert report.merged_rows == []


# ---------------------------------------------------------------------------
# 6) 新增/新增冲突
# ---------------------------------------------------------------------------

def test_add_add_with_same_key_different_payload_conflicts():
    dev = [row(9, "ivan", "nj", 90)]
    main = [row(9, "ivan", "nj", 99)]
    report = _merge([], dev, main)
    d = _auto_map(report)[9]
    assert d.decision is RowDecision.ADD_ADD_CONFLICT
    assert report.merged_rows == []


def test_add_add_identical_converges():
    payload = [row(9, "ivan", "nj", 90)]
    report = _merge([], payload, payload)
    assert _auto_map(report)[9].decision is RowDecision.FIELD_MERGE
    assert report.merged_rows == payload


# ---------------------------------------------------------------------------
# 7) 解决动作的合法/非法类别
# ---------------------------------------------------------------------------

def test_resolution_action_validation_categories():
    # 合法组合不抛
    validate_resolution(RowDecision.SAME_FIELD_CONFLICT, ResolutionAction.USE_MAIN)
    validate_resolution(RowDecision.SAME_FIELD_CONFLICT, ResolutionAction.FIELD_PICK,
                        {"score": "MAIN"})
    validate_resolution(RowDecision.DELETE_MODIFY_CONFLICT, ResolutionAction.KEEP_DELETED)
    validate_resolution(RowDecision.ADD_ADD_CONFLICT, ResolutionAction.USE_DEV)

    # 删除/修改冲突不允许 FIELD_PICK
    with pytest.raises(InvalidResolutionError) as ei:
        validate_resolution(RowDecision.DELETE_MODIFY_CONFLICT,
                            ResolutionAction.FIELD_PICK, {"score": "DEV"})
    assert ei.value.error_code == "INVALID_RESOLUTION"
    # 新增/新增冲突不允许 KEEP_DELETED
    with pytest.raises(InvalidResolutionError):
        validate_resolution(RowDecision.ADD_ADD_CONFLICT, ResolutionAction.KEEP_DELETED)
    # 非冲突行不需要解决
    with pytest.raises(InvalidResolutionError):
        validate_resolution(RowDecision.UNCHANGED, ResolutionAction.USE_DEV)
    # FIELD_PICK 缺映射 / 错误取值
    with pytest.raises(InvalidResolutionError):
        validate_resolution(RowDecision.SAME_FIELD_CONFLICT, ResolutionAction.FIELD_PICK)
    with pytest.raises(InvalidResolutionError):
        validate_resolution(RowDecision.SAME_FIELD_CONFLICT, ResolutionAction.FIELD_PICK,
                            {"score": "BASE"})
    # 未知动作字符串
    with pytest.raises(InvalidResolutionError):
        validate_resolution(RowDecision.SAME_FIELD_CONFLICT, "TAKE_OWN")  # type: ignore[arg-type]


def test_apply_resolutions_requires_every_conflict():
    # dev 删除 4，main 修改 4 -> 单个删除/修改冲突；不给解决必须报“未解决”
    base = [row(4, "d", "hz", 40)]
    report = _merge(base, [], [row(4, "d", "hf", 41)])
    assert len(report.conflicts) == 1
    with pytest.raises(InvalidResolutionError) as ei:
        apply_resolutions(SCHEMA, report, {})
    assert "[4]" in ei.value.details["unresolved"][0]

    # 解决表里夹带非冲突键也要明确失败，而不是被忽略
    with pytest.raises(InvalidResolutionError) as ei:
        apply_resolutions(SCHEMA, report, {
            (4,): {"action": ResolutionAction.USE_MAIN},
            (999,): {"action": ResolutionAction.USE_DEV},
        })
    assert "not conflicts" in ei.value.message


def test_apply_resolutions_end_to_end_specific_rows():
    # id=4 删除/修改（dev 删 / main 改）-> USE_MAIN 保留 main 的修改
    # id=6 同字段冲突 -> USE_DEV
    # id=9 新增/新增 -> USE_MAIN
    base = [row(1, "a", "bj", 10), row(4, "d", "hz", 40),
            row(6, "f", "wh", 60), row(2, "b", "sh", 20)]
    dev = [row(1, "a", "bj", 10), row(6, "f", "wh", 66),
           row(2, "b", "sh", 20), row(7, "g", "sz", 70),
           row(9, "ivan", "nj", 90)]
    main = [row(1, "a", "bj", 10), row(4, "d", "hf", 41),
            row(6, "f", "wh", 99), row(2, "b", "sh", 20),
            row(9, "ivan", "nj", 99)]
    report = _merge(base, dev, main)
    keys = {d.key[0]: d for d in report.conflicts}
    assert set(keys) == {4, 6, 9}

    final = apply_resolutions(SCHEMA, report, {
        (4,): {"action": ResolutionAction.USE_MAIN},
        (6,): {"action": ResolutionAction.USE_DEV},
        (9,): {"action": ResolutionAction.USE_MAIN},
    })
    by_id = {r["id"]: r for r in final}
    assert set(by_id) == {1, 2, 4, 6, 7, 9}       # 自动行 + 解决行
    assert by_id[4] == row(4, "d", "hf", 41)     # main 的修改保留
    assert by_id[6] == row(6, "f", "wh", 66)     # dev 值
    assert by_id[9] == row(9, "ivan", "nj", 99)  # main 的新增
    assert by_id[7] == row(7, "g", "sz", 70)     # dev 独占新增自动合入

    # 同一计划改用 KEEP_DELETED：id=4 必须消失
    final2 = apply_resolutions(SCHEMA, report, {
        (4,): {"action": ResolutionAction.KEEP_DELETED},
        (6,): {"action": ResolutionAction.FIELD_PICK,
               "field_picks": {"id": "DEV", "name": "DEV", "city": "DEV",
                               "score": "MAIN", "active": "MAIN"}},
        (9,): {"action": ResolutionAction.USE_DEV},
    })
    by_id2 = {r["id"]: r for r in final2}
    assert 4 not in by_id2
    assert by_id2[6] == row(6, "f", "wh", 99, active=True)  # score/active 取 main
    assert by_id2[9] == row(9, "ivan", "nj", 90)


def test_field_pick_takes_per_field_sides():
    base = [row(6, "f", "wh", 60)]
    dev = [row(6, "f", "bj", 66)]
    main = [row(6, "frank", "gz", 99)]
    report = _merge(base, dev, main)
    final = apply_resolutions(SCHEMA, report, {
        (6,): {"action": ResolutionAction.FIELD_PICK,
               "field_picks": {"name": "MAIN", "city": "DEV",
                               "score": "MAIN", "active": "MAIN", "id": "MAIN"}},
    })
    assert final == [row(6, "frank", "bj", 99)]


# ---------------------------------------------------------------------------
# 8) schema 必须三方一致，否则给明确失败类别（不是成功也不是 KeyError）
# ---------------------------------------------------------------------------

def test_schema_mismatch_raises_classified_error():
    other = TableSchema(
        table="employees",
        columns=(Column("id", "int64"), Column("name", "string"),
                 Column("city", "string")),  # 少了 score/active
        primary_key=("id",),
    )
    snap_other = Snapshot("O", "employees", other, "o.parquet", 0, "o")
    with pytest.raises(SchemaMismatchError) as ei:
        ensure_compatible_schemas(_snap("B"), _snap("D"), snap_other)
    assert ei.value.error_code == "SCHEMA_MISMATCH"
    assert ei.value.details["main_snapshot_id"] == "O"


def test_steps_record_progress_and_basis():
    report = _merge([row(1, "a", "x", 1)], [row(1, "a", "y", 1)],
                    [row(1, "a", "x", 1)])
    assert len(report.steps) == 4
    assert any("step 1/4" in s and "primary key" in s for s in report.steps)
    assert any("classified" in s for s in report.steps)
    d = _auto_map(report)[1]
    assert "only dev changed" in d.basis


# ---------------------------------------------------------------------------
# 9) 与独立 oracle 的逐键一致性（手写小矩阵全枚举）
# ---------------------------------------------------------------------------

def test_kernel_matches_independent_oracle_on_hand_matrix():
    base = [
        row(1, "a", "x", 1),                      # 双方不动
        row(2, "b", "x", 2, active=True),         # 不相交字段
        row(3, "c", "x", 3),                      # dev 删 / main 不动
        row(4, "d", "x", 4),                      # dev 删 / main 改
        row(5, "e", "x", 5),                      # main 删 / dev 改
        row(6, "f", "x", 6, active=True),         # 同字段冲突
        row(10, "z", "x", 10),                    # 双方都删
    ]
    dev = [
        row(1, "a", "x", 1),
        row(2, "b", "bj", 2, active=True),
        row(5, "e", "bj", 5),
        row(6, "f", "x", 66, active=False),
        row(7, "g", "x", 7),                      # 仅 dev 新增
        row(9, "i", "x", 90),                     # 双方新增不同内容
    ]
    main = [
        row(1, "a", "x", 1),
        row(2, "b", "x", 2, active=False),
        row(3, "c", "x", 3),
        row(4, "d", "hf", 4),
        row(6, "f", "x", 99, active=False),
        row(8, "h", "x", 8),                      # 仅 main 新增
        row(9, "i", "x", 99),
    ]
    expected = oracle_merge(list(EMP_COLUMNS), ("id",), base, dev, main)
    report = _merge(base, dev, main)

    assert {d.key for d in report.decisions} == set(expected)
    for detail in report.decisions:
        exp_decision, exp_row = expected[detail.key]
        assert detail.decision.value == exp_decision, (
            f"key {key_string(detail.key)}: kernel={detail.decision.value} "
            f"oracle={exp_decision}"
        )
        if not detail.decision.is_conflict and exp_row is not None:
            # 自动结果行必须与 oracle 字面量逐字段相等
            actual = next(r for r in report.merged_rows
                          if tuple(r[c] for c in ("id",)) == detail.key)
            assert actual == exp_row

    # 显式断言各分类在该矩阵中都至少出现一次（防止某分支根本没被测到）
    seen = {d.decision.value for d in report.decisions}
    assert {UNCHANGED, FAST_FORWARD, FIELD_MERGE, SAME_FIELD_CONFLICT,
            DELETE_MODIFY_CONFLICT, ADD_ADD_CONFLICT} <= seen
