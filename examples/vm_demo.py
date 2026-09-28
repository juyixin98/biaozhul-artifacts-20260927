#!/usr/bin/env python3
"""不依赖 HTTP 服务的核心演示：gas 临界点、写后异常回滚、跨进程确定性。

    .venv/bin/python examples/vm_demo.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from teaching_chain.vm import Failure, assemble, execute  # noqa: E402


def show(title: str, result) -> None:
    print(f"\n=== {title} ===")
    print(f"ok={result.ok} gas_used={result.gas_used} "
          f"error={result.error_category} pc={result.error_pc}")
    print(f"storage={result.storage}")
    if result.trace:
        for line in result.trace:
            print("  " + line)


def main() -> int:
    # 1) 临界 gas：程序固定费用 9，给 9 成功、给 8 在 ADD 前 OOG（ADD 不发生）
    code = assemble("PUSH8 3\nPUSH8 2\nADD\nSTOP")
    show("恰好 9 gas：成功且全耗", execute(code, 9, trace=True))
    show("只有 8 gas：ADD 执行前 OOG，费用保留", execute(code, 8))

    # 2) 状态写后异常：SSTORE 成功后除零 => 存储回滚、整帧 gas 保留
    code_rw = assemble(
        "PUSH8 7\nPUSH8 1\nSSTORE\n"
        "PUSH8 1\nPUSH8 0\nDIV\nSTOP"
    )
    show("写后除零：状态回滚 + gas 全耗", execute(code_rw, 100_000, storage={5: 999}))

    # 3) 整数边界：2^63-1 + 1
    code_ov = assemble(f"PUSH8 {2**63 - 1}\nPUSH8 1\nADD\nSTOP")
    show("整数上界 +1：INTEGER_OVERFLOW", execute(code_ov, 100))

    # 4) 嵌套调用失败：子帧 REVERT，父子写入全部回滚，gas 全耗
    code_call = assemble("""
        PUSH8 42
        PUSH8 1
        SSTORE
        CALL
        {
            PUSH8 7
            PUSH8 2
            SSTORE
            REVERT
        }
        STOP
    """)
    result = execute(code_call, 100_000, trace=True)
    show("嵌套子帧 REVERT：隔离回滚", result)
    assert result.storage == {}
    assert result.error_category == str(Failure.REVERTED)
    print("\n全部断言通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
