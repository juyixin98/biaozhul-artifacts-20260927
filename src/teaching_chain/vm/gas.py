"""Gas 价格表（教学版，数值固定且公开）。

费用规则（README 有完整说明）：

* 每条指令先检查剩余 gas、再扣费、最后才执行副作用；
* 线性内存按 32 字节“字”扩张，扩张费采用 ``3*words + words^2/512``
  的增量计费（只对新增部分收费）；
* 帧异常停机时消耗该帧被分配的全部 gas（状态回滚、费用保留）；
* 交易固有费用（intrinsic）见 ``intrinsic_gas``。

这些数值刻意采用小整数，便于在测试中手算核验。
"""
from __future__ import annotations

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
UINT64_MOD = 1 << 64

STACK_LIMIT = 1024
MEMORY_WORD_SIZE = 32
MEMORY_MAX_END = 1 << 31  # 单次访问末端上限（教学上限，远小于真实需求即可）

# 指令费用
G_ZERO = 0
G_JUMP = 0
G_BASE = 2        # POP 等最简操作
G_VERY_LOW = 3    # 算术 / 比较 / 压栈 / 栈操作 / 内存指令基础费
G_LOW = 5         # SLOAD、DIV/MOD
G_SSTORE = 20     # 存储写（固定费，无退款——教学简化，见 README 取舍）
G_CALL_BASE = 10  # 嵌套调用基础费（无论子调用成败都收取）

# 内存扩张
G_MEMORY_PER_WORD = 3
G_MEMORY_QUAD_DIVISOR = 512

# 交易固有费用
G_TX_MINIMUM = 21
G_TX_ZERO_BYTE = 1
G_TX_NONZERO_BYTE = 4

# API 提交交易的默认 gas 上限（仅缺省时使用）
DEFAULT_TX_GAS = 100_000


def memory_expansion_cost(previous_words: int, new_words: int) -> int:
    """从 previous_words 扩张到 new_words 的**增量**费用。"""
    if new_words <= previous_words:
        return 0

    def total(words: int) -> int:
        return G_MEMORY_PER_WORD * words + words * words // G_MEMORY_QUAD_DIVISOR

    return total(new_words) - total(previous_words)


def intrinsic_gas(code: bytes) -> int:
    """交易固有费用：21 + 每字节费用（零字节 1，非零字节 4）。"""
    data_cost = sum(G_TX_ZERO_BYTE if b == 0 else G_TX_NONZERO_BYTE for b in code)
    return G_TX_MINIMUM + data_cost
