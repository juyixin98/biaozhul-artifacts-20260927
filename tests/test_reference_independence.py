"""元测试：参考答案生成器绝不允许依赖被测核心。

用 AST 解析 tools/generate_fixtures.py 的所有 import 语句，断言顶层包名
不含 rsv。这防止后续维护者无意中让“参考答案由被测核心自身生成”。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_fixture_generator_does_not_import_system_under_test():
    src = (ROOT / "tools" / "generate_fixtures.py").read_text("utf-8")
    tree = ast.parse(src)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.append(node.module.split(".")[0])
    assert "rsv" not in imported, f"generator must stay independent, got {imported}"
    # 第三方依赖只允许成熟密码库；其余必须是标准库
    allowed_third_party = {"cryptography"}
    stdlib_top = {
        "hashlib", "json", "pathlib", "dataclasses", "typing", "__future__",
        "os", "sys", "argparse", "importlib", "tempfile", "datetime", "uuid",
        "sqlite3", "contextlib", "collections",
    }
    third_party = {m for m in imported if m not in stdlib_top}
    assert third_party <= allowed_third_party, third_party
