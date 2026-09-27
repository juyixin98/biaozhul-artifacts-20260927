"""强制参考实现独立性：oracle 不得导入任何 app.* 模块。

参考答案不能全部由被测核心实现自身生成——本测试从源码层面保证
tests/_oracle.py 是一份独立的集合代数实现。
"""
from __future__ import annotations

import pathlib
import re

from tests import _oracle

ORACLE_PATH = pathlib.Path(_oracle.__file__)


def test_oracle_source_does_not_import_app_modules():
    src = ORACLE_PATH.read_text(encoding="utf-8")
    # 去掉注释行后再检查，避免“文档里提到 import app”这种误伤
    code_lines = [
        ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)
    assert not re.search(r"^\s*from\s+app\b", code, re.MULTILINE), (
        "oracle 禁止 from app... 导入被测实现"
    )
    assert not re.search(r"^\s*import\s+app\b", code, re.MULTILINE), (
        "oracle 禁止 import app"
    )
    # 也不能 import tests 下的其他辅助（那些会碰存储）
    assert "from app" not in code
    assert "import app" not in code


def test_oracle_self_consistency_basic_sets():
    """oracle 自身行为的最小健全性（不经过任何 app 代码）。"""
    terms = {"a": {1, 2, 3}, "b": {2, 3, 4}}
    universe = {1, 2, 3, 4, 5}
    assert _oracle.oracle_answer("a AND b", terms, universe) == {2, 3}
    assert _oracle.oracle_answer("a OR b", terms, universe) == {1, 2, 3, 4}
    assert _oracle.oracle_answer("NOT a", terms, universe) == {4, 5}
    assert _oracle.oracle_answer("*", terms, universe) == {1, 2, 3, 4, 5}
    # 空全集：NOT 也是空
    assert _oracle.oracle_answer("NOT a", terms, set()) == set()
    # universe 之外的整数永远不会因 NOT 出现
    result = _oracle.oracle_answer("NOT b", terms, {1, 2})
    assert result <= {1, 2}
