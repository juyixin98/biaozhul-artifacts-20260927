"""内核单元测试：逐键分类、字段级合并、冲突类别、解决判定。

断言具体结果（具体行、具体字段来源、具体失败类别），不只检查"接口能调用"。
自动分区的整体结论同时与独立参考实现 tests.reference_oracle 交叉验证。
"""
from __future__ import annotations

import itertools
import json

import pytest

from merge3.domain.models import EntryClass, FieldOrigin, ResolutionKind, TableSpec, FieldSpec
from merge3.kernel import diff3
from merge3.errors import ResolutionRejectedError, ValidationError

SPEC = TableSpec(
    name="orders",
    primary_key=["id"],
    fields=[FieldSpec("id", "int64", False), FieldSpec("status", "string"),
            FieldSpec("amount", "int64"), FieldSpec("owner", "string")],
)
COLS = ["id", "status", "amount", "owner"]


def plan(base, ours, theirs):
    return diff3.build_plan(SPEC, base, ours, theirs, "snap_base", "snap_ours",
                            "snap_theirs")


def entry(p, key):
    return p.entries[json.dumps([key], separators=(",", ":"))]


# ------------------------------------------------------------ 1. 相同修改

class TestSameModification:
    def test_both_same_edit_is_auto(self):
        base = [{"id": 1, "status": "new", "amount": 100, "owner": "a"}]
        ours = [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}]
        theirs = [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}]
        e = entry(plan(base, ours, theirs), 1)
        assert e.classification == EntryClass.FIELD_MERGE.value
        assert e.conflict is False
        assert e.merged["status"] == "paid"
        assert e.fields["status"].origin == FieldOrigin.AGREED.value

    def test_unchanged(self):
        r = {"id": 1, "status": "new", "amount": 10, "owner": "x"}
        e = entry(plan([r], [dict(r)], [dict(r)]), 1)
        assert e.classification == EntryClass.UNCHANGED.value
        assert e.merged == r


# ------------------------------------------------------------ 2. 互斥新增

class TestDisjointAdds:
    def test_each_side_adds_distinct_keys(self):
        base = [{"id": 1, "status": "new", "amount": 10, "owner": "a"}]
        ours = base + [{"id": 2, "status": "new", "amount": 20, "owner": "d"}]
        theirs = base + [{"id": 3, "status": "new", "amount": 30, "owner": "m"}]
        p = plan(base, ours, theirs)
        assert not p.conflicts
        assert entry(p, 2).classification == EntryClass.OURS_ADDED.value
        assert entry(p, 3).classification == EntryClass.THEIRS_ADDED.value
        assert {e.merged["id"] for e in p.entries.values() if not e.deleted} == {1, 2, 3}

    def test_same_key_identical_add(self):
        ours = [{"id": 9, "status": "new", "amount": 1, "owner": "x"}]
        theirs = [dict(ours[0])]
        e = entry(plan([], ours, theirs), 9)
        assert e.classification == EntryClass.ADD_ADD_IDENTICAL.value
        assert e.conflict is False


# ------------------------------------------------------------ 3. 同键不同字段 / 同键同字段不同值

class TestSameKeyDifferentFields:
    def test_disjoint_field_edits_merge_fieldwise(self):
        # 行为约定1的核心：比较记录身份与字段，而非文件名/行序
        base = [{"id": 1, "status": "new", "amount": 100, "owner": "a"}]
        ours = [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}]
        theirs = [{"id": 1, "status": "new", "amount": 100, "owner": "b"}]
        e = entry(plan(base, ours, theirs), 1)
        assert e.classification == EntryClass.FIELD_MERGE.value
        assert e.merged == {"id": 1, "status": "paid", "amount": 100, "owner": "b"}
        assert e.fields["status"].origin == FieldOrigin.OURS.value
        assert e.fields["owner"].origin == FieldOrigin.THEIRS.value
        assert e.fields["amount"].origin == FieldOrigin.BASE.value

    def test_same_field_different_values_is_conflict_with_details(self):
        base = [{"id": 1, "status": "new", "amount": 100, "owner": "a"}]
        ours = [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}]
        theirs = [{"id": 1, "status": "void", "amount": 100, "owner": "a"}]
        p = plan(base, ours, theirs)
        e = entry(p, 1)
        assert e.classification == EntryClass.FIELD_VALUE_CONFLICT.value
        assert e.conflicting_fields == ["status"]
        assert "status" in e.reason and "paid" in e.reason and "void" in e.reason
        # 非冲突字段仍保留祖先值并标记冲突待决
        assert e.fields["status"].origin == FieldOrigin.CONFLICT.value
        assert p.unresolved["[1]"].classification == "field_value_conflict"

    def test_value_conflict_on_one_field_keeps_other_field_auto_merge(self):
        # dev 改 status；main 同时改 status 和 amount 的不同值：
        # status 冲突，但 amount 只有 main 改 -> 自动并入
        base = [{"id": 1, "status": "new", "amount": 100, "owner": "a"}]
        ours = [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}]
        theirs = [{"id": 1, "status": "void", "amount": 250, "owner": "a"}]
        e = entry(plan(base, ours, theirs), 1)
        assert e.conflicting_fields == ["status"]
        assert e.merged["amount"] == 250
        assert e.fields["amount"].origin == FieldOrigin.THEIRS.value


# ------------------------------------------------------------ 4. 删除/修改冲突

class TestDeleteModify:
    def test_dev_deletes_main_modifies_is_conflict(self):
        base = [{"id": 1, "status": "new", "amount": 100, "owner": "a"}]
        ours = []
        theirs = [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}]
        p = plan(base, ours, theirs)
        e = entry(p, 1)
        assert e.classification == EntryClass.DELETE_MODIFY_CONFLICT.value
        assert e.merged is None and e.deleted is False
        assert e.ours_row is None and e.theirs_row is not None

    def test_main_deletes_dev_modifies_is_conflict(self):
        base = [{"id": 1, "status": "new", "amount": 100, "owner": "a"}]
        ours = [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}]
        theirs = []
        e = entry(plan(base, ours, theirs), 1)
        assert e.classification == EntryClass.DELETE_MODIFY_CONFLICT.value

    def test_both_delete_is_auto_delete(self):
        r = {"id": 1, "status": "new", "amount": 100, "owner": "a"}
        e = entry(plan([r], [], []), 1)
        assert e.classification == EntryClass.BOTH_DELETED.value
        assert e.deleted is True and e.merged is None

    def test_one_side_delete_other_untouched_is_auto_delete(self):
        r = {"id": 1, "status": "new", "amount": 100, "owner": "a"}
        assert entry(plan([r], [], [dict(r)]), 1).classification == "ours_deleted"
        assert entry(plan([r], [dict(r)], []), 1).classification == "theirs_deleted"


# ------------------------------------------------------------ 单边修改

class TestSingleSideModify:
    def test_ours_only(self):
        r = {"id": 1, "status": "new", "amount": 10, "owner": "a"}
        o = {"id": 1, "status": "paid", "amount": 10, "owner": "a"}
        e = entry(plan([r], [o], [dict(r)]), 1)
        assert e.classification == EntryClass.OURS_MODIFIED.value
        assert e.merged == o

    def test_theirs_only(self):
        r = {"id": 1, "status": "new", "amount": 10, "owner": "a"}
        t = {"id": 1, "status": "paid", "amount": 10, "owner": "a"}
        e = entry(plan([r], [dict(r)], [t]), 1)
        assert e.classification == EntryClass.THEIRS_MODIFIED.value
        assert e.merged == t


# ------------------------------------------------------------ 冲突解决（纯判定）

class TestResolutions:
    def _conflict_entry(self, cls):
        p = {
            "field_value_conflict": (
                [{"id": 1, "status": "new", "amount": 100, "owner": "a"}],
                [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}],
                [{"id": 1, "status": "void", "amount": 100, "owner": "a"}],
            ),
            "delete_modify": (
                [{"id": 1, "status": "new", "amount": 100, "owner": "a"}],
                [],
                [{"id": 1, "status": "paid", "amount": 100, "owner": "a"}],
            ),
            "add_add": (
                [],
                [{"id": 7, "status": "new", "amount": 10, "owner": "d"}],
                [{"id": 7, "status": "new", "amount": 20, "owner": "m"}],
            ),
        }[cls]
        e = list(plan(*p).entries.values())[0]
        assert e.conflict
        return e

    def test_take_ours_theirs_delete_keep(self):
        e = self._conflict_entry("delete_modify")
        row, deleted = diff3.apply_resolution(e, ["id"], ResolutionKind.OURS.value, None)
        assert deleted is True  # ours 侧是删除
        row, deleted = diff3.apply_resolution(e, ["id"], ResolutionKind.THEIRS.value, None)
        assert deleted is False and row["status"] == "paid"
        row, deleted = diff3.apply_resolution(e, ["id"], ResolutionKind.DELETE.value, None)
        assert deleted is True
        row, _ = diff3.apply_resolution(e, ["id"], ResolutionKind.KEEP.value, None)
        assert row["status"] == "paid"

    def test_keep_rejected_for_value_conflict(self):
        e = self._conflict_entry("field_value_conflict")
        with pytest.raises(ResolutionRejectedError):
            diff3.apply_resolution(e, ["id"], ResolutionKind.KEEP.value, None)

    def test_value_must_cover_all_clashing_fields(self):
        e = self._conflict_entry("field_value_conflict")
        with pytest.raises(ResolutionRejectedError):
            diff3.apply_resolution(e, ["id"], ResolutionKind.VALUE.value, {})

    def test_value_rejects_touching_non_conflict_field(self):
        e = self._conflict_entry("field_value_conflict")
        with pytest.raises(ResolutionRejectedError):
            diff3.apply_resolution(
                e, ["id"], ResolutionKind.VALUE.value,
                {"status": "paid", "amount": 999},
            )

    def test_value_field_conflict_merges_and_overrides(self):
        e = self._conflict_entry("field_value_conflict")
        row, deleted = diff3.apply_resolution(
            e, ["id"], ResolutionKind.VALUE.value, {"status": "shipped"})
        assert not deleted
        assert row == {"id": 1, "status": "shipped", "amount": 100, "owner": "a"}

    def test_value_add_add_requires_full_row_and_pins_pk(self):
        e = self._conflict_entry("add_add")
        with pytest.raises(ResolutionRejectedError):
            diff3.apply_resolution(e, ["id"], ResolutionKind.VALUE.value, {"amount": 5})
        row, _ = diff3.apply_resolution(
            e, ["id"], ResolutionKind.VALUE.value,
            {"id": 999, "status": "new", "amount": 5, "owner": "z"})
        assert row["id"] == 7  # 主键身份不允许被解决方案改变

    def test_unknown_kind_rejected(self):
        e = self._conflict_entry("add_add")
        with pytest.raises(ResolutionRejectedError):
            diff3.apply_resolution(e, ["id"], "coinflip", None)


# ------------------------------------------------------------ Schema 不兼容

def test_schema_compat_check():
    other = TableSpec("orders", ["id"], [FieldSpec("id", "int64", False)])
    with pytest.raises(ValidationError):
        diff3.assert_same_schema(SPEC, other, "测试")


# ------------------------------------------------------------ 与独立参考实现交叉验证

def test_kernel_matches_independent_oracle_across_enumerated_states():
    """对小状态空间枚举：同一条记录在三方各取 3 种状态（删/原值/两个变体），
    被测内核的自动结果行集与冲突分类必须等于独立参考实现。"""
    from tests.reference_oracle import reference_merge

    statuses = {
        "absent": None,
        "v0": {"id": 5, "status": "new", "amount": 100, "owner": "a"},
        "v1": {"id": 5, "status": "paid", "amount": 100, "owner": "a"},   # 改 status
        "v2": {"id": 5, "status": "new", "amount": 200, "owner": "a"},    # 改 amount
        "v3": {"id": 5, "status": "void", "amount": 300, "owner": "a"},   # 同字段不同值
    }
    states = list(statuses)
    mismatches = []
    for sb, so, st in itertools.product(states, repeat=3):
        b, o, t = statuses[sb], statuses[so], statuses[st]
        base = [b] if b else []
        ours = [o] if o else []
        theirs = [t] if t else []
        p = plan(base, ours, theirs)
        ref = reference_merge(COLS, ["id"], base, ours, theirs)
        got_rows = sorted(
            (json.dumps(e.merged, sort_keys=True)
             for e in p.entries.values() if not e.deleted and not e.conflict)
        )
        want_rows = sorted(json.dumps(r, sort_keys=True) for r in ref["rows"])
        got_conf = {
            k: e.classification for k, e in p.entries.items() if e.conflict
        }
        want_conf = {}
        for k, c in ref["conflicts"].items():
            # 参考键 repr 元组 -> 内核 JSON 键
            want_conf[json.dumps([5], separators=(",", ":"))] = c
        if got_rows != want_rows or set(got_conf.values()) != set(want_conf.values()):
            mismatches.append((sb, so, st, got_rows, want_rows, got_conf, want_conf))
    assert not mismatches, f"内核与参考实现不一致: {mismatches[:3]}"
