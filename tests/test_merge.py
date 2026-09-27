"""三路合并测试:以手写夹具为参考答案,断言具体合并结果与冲突结构。

参考答案全部手写于 fixtures/*.json,不由被测实现生成。
"""

import pytest

from app.merge import UnknownChoiceError, UnresolvedConflictError, merge3, resolve3
from tests.conftest import load_fixture


def test_merge_matches_handwritten_expectation(merge_fixture):
    outcome = merge3(
        merge_fixture["base"], merge_fixture["local"], merge_fixture["remote"]
    )
    expected = merge_fixture["expected"]
    assert outcome.status == expected["status"]
    assert outcome.text == expected["text"]
    assert len(outcome.conflicts) == len(expected["conflicts"])
    for conflict, want in zip(outcome.conflicts, expected["conflicts"]):
        assert conflict.kind == want["kind"]
        assert [conflict.base_start, conflict.base_end] == want["base_range"]
        assert [conflict.local.start, conflict.local.end] == want["local_range"]
        assert [conflict.remote.start, conflict.remote.end] == want["remote_range"]
    for substring in expected["notes_include"]:
        assert any(substring in note for note in outcome.notes), (
            f"missing note containing {substring!r}; got {outcome.notes}"
        )


def test_conflicts_rebuildable_by_explicit_choice(merge_fixture):
    resolutions = merge_fixture["resolutions"]
    if not resolutions:
        pytest.skip("clean fixture has no conflicts to resolve")
    for side, want_text in resolutions.items():
        outcome = merge3(
            merge_fixture["base"], merge_fixture["local"], merge_fixture["remote"]
        )
        choices = {i: side for i in range(len(outcome.conflicts))}
        rebuilt = resolve3(
            merge_fixture["base"], merge_fixture["local"], merge_fixture["remote"],
            choices,
        )
        assert rebuilt == want_text, f"resolution {side!r} mismatch"


def test_clean_merge_contains_both_sides_edits():
    # 无冲突合并必须与两侧独立编辑一致:对结果分别与 base 求 diff,
    # 两侧的修改都应体现在结果中(独立于合并实现自身的交叉检验)。
    from app.edits import compute_edits
    from app.textnorm import split_lines

    for name in ("disjoint-edits", "moved-paragraph", "duplicate-lines",
                 "crlf-preserved", "identical-edits"):
        fx = load_fixture(name)
        outcome = merge3(fx["base"], fx["local"], fx["remote"])
        assert outcome.status == "clean"
        base_lines = split_lines(fx["base"])
        result_lines = split_lines(outcome.text)
        # 结果中应能找到两侧各自引入的新行
        for side in ("local", "remote"):
            for edit in compute_edits(base_lines, split_lines(fx[side])):
                for line in edit.replacement:
                    assert line in result_lines, (
                        f"{name}: {side} edit line {line!r} missing from result"
                    )


def test_resolve_requires_all_choices():
    fx = load_fixture("same-point-insert")
    with pytest.raises(UnresolvedConflictError) as excinfo:
        resolve3(fx["base"], fx["local"], fx["remote"], {})
    assert excinfo.value.missing == [0]


def test_resolve_rejects_unknown_choice():
    fx = load_fixture("same-point-insert")
    with pytest.raises(UnknownChoiceError):
        resolve3(fx["base"], fx["local"], fx["remote"], {0: "sideways"})


def test_merge_is_deterministic():
    fx = load_fixture("duplicate-lines")
    first = merge3(fx["base"], fx["local"], fx["remote"])
    second = merge3(fx["base"], fx["local"], fx["remote"])
    assert first.text == second.text


def test_insert_at_edit_boundary_is_not_a_conflict():
    # 一方在另一方编辑区间的边界处插入:按确定性规则干净合并(插入先于替换)
    base = "a\nb\nc\n"
    local = "a\nX\nb\nc\n"   # 在位置 1 插入
    remote = "a\nB\nc\n"     # 替换位置 1 的行
    outcome = merge3(base, local, remote)
    assert outcome.status == "clean"
    assert outcome.text == "a\nX\nB\nc\n"
