"""区间编辑测试:编辑区间的具体内容与边界语义。"""

from app.edits import Edit, compute_edits
from app.textnorm import split_lines


def edits(base: str, other: str) -> list[Edit]:
    return compute_edits(split_lines(base), split_lines(other))


def test_modify_single_line():
    result = edits("a\nb\nc\n", "a\nB\nc\n")
    assert result == [Edit(1, 2, ("B\n",))]


def test_insert_lines():
    result = edits("a\nc\n", "a\nb\nc\n")
    assert result == [Edit(1, 1, ("b\n",))]
    assert result[0].is_insert


def test_delete_lines():
    result = edits("a\nb\nc\n", "a\n")
    assert result == [Edit(1, 3, ())]
    assert result[0].is_delete


def test_replace_block():
    result = edits("a\nb\nc\nd\n", "a\nX\nY\nd\n")
    assert result == [Edit(1, 3, ("X\n", "Y\n"))]


def test_no_edits_for_identical_text():
    assert edits("a\nb\n", "a\nb\n") == []


def test_terminator_flavor_change_produces_no_edit():
    # 行内容相同、仅 \n -> \r\n:不产生编辑(诊断层会单独说明)
    assert edits("a\nb\n", "a\r\nb\r\n") == []


def test_trailing_newline_change_is_an_edit():
    # 末尾换行状态变化必须被识别为编辑,否则追加行会与未终止末行粘连
    result = edits("a\nb", "a\nb\n")
    assert result == [Edit(1, 2, ("b\n",))]
    result = edits("a\nb\n", "a\nb")
    assert result == [Edit(1, 2, ("b",))]


def test_append_to_unterminated_file():
    result = edits("one\ntwo", "one\ntwo\nthree\n")
    assert result == [Edit(1, 2, ("two\n", "three\n"))]


def test_duplicate_lines_deterministic():
    # 重复行场景下删除位置是确定的(最长匹配保留后缀)
    result = edits("s\ns\ns\nu\n", "s\ns\nu\n")
    assert result == [Edit(0, 1, ())]


def test_multiple_disjoint_edits_ordered():
    result = edits("a\nb\nc\nd\ne\n", "A\nb\nc\nD\ne\nf\n")
    assert result == [Edit(0, 1, ("A\n",)), Edit(3, 4, ("D\n",)), Edit(5, 5, ("f\n",))]
