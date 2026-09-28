"""Gas 价格表 v1 与内存扩张费用公式。

费用规则（教学链，数值借鉴 EVM 但刻意简化，全部为显式常量）：

* 每笔交易先收内在费用（intrinsic），不足直接在准入阶段拒绝，不产生副作用；
* 部署额外收 DEPLOY_BASE，并按代码字节收字节费；调用按输入字数收字节费；
* 每条操作码在“执行副作用之前”按本表扣费，剩余不足即 out_of_gas；
* 内存按 u64 字扩张，费用随字数二次增长：``cost(w) = 3*w + w*w // 512``；
* SSTORE 的 set/reset/清零退费用独立函数计算（见 :func:`sstore_cost`）。

该模块**不允许**依赖任何宿主时间/随机源（tests/test_determinism_contracts.py 会扫描）。
"""

from __future__ import annotations

# ---- 内在费用（准入时收取，先于一切副作用） ----
INTRINSIC_TX = 21_000
TX_INPUT_WORD_COST = 4        # invoke: 每个输入 u64 字
TX_CODE_BYTE_COST = 4         # deploy: 每个代码字节
DEPLOY_BASE = 32_000          # deploy 固定附加

# ---- 操作码费用（执行该操作之前扣减） ----
COST_STOP = 0
COST_JUMPDEST = 1
COST_POP = 2
COST_PUSH = 3
COST_DUP = 3
COST_SWAP = 3
COST_MLOAD = 3
COST_MSTORE = 3
COST_MSIZE = 2
COST_ARITH = 5                # ADD SUB MUL DIV MOD
COST_COMPARE = 3              # LT GT EQ ISZERO
COST_CALLDATALOAD = 3
COST_CALLDATASIZE = 2
COST_JUMP = 8
COST_JUMPI = 10
COST_SLOAD = 800
COST_CALL = 700
COST_RETURN = 0
COST_REVERT = 0

# ---- SSTORE 简化计费（工作集口径，非 EIP-2200） ----
SSTORE_NOOP = 200             # 新值等于当前值
SSTORE_SET = 20_000           # 0 -> 非 0
SSTORE_RESET = 5_000          # 非 0 -> 其它值（含清零）
SSTORE_CLEAR_REFUND = 15_000  # 非 0 -> 0 的退款（受退款上限约束）

# ---- 其它常量 ----
MAX_STACK = 1024
REFUND_FACTOR_DENOM = 2       # 退款上限：已消耗 gas 的 1/2


def memory_cost(words: int) -> int:
    """扩张到 ``words`` 个字时的累计内存费用。"""
    return 3 * words + words * words // 512


def memory_expansion_delta(old_words: int, new_words: int) -> int:
    """从 old_words 扩张到 new_words 需要补收的费用（不扩张为 0）。"""
    if new_words <= old_words:
        return 0
    return memory_cost(new_words) - memory_cost(old_words)


def sstore_cost(current: int, new: int) -> tuple[int, int]:
    """返回 (本次扣费, 退款增量)。

    current 为该槽位在当前工作集里的值（已考虑祖先帧已提交的写）。
    """
    if new == current:
        return SSTORE_NOOP, 0
    if current == 0:
        return SSTORE_SET, 0
    # current != 0 且 new != current
    if new == 0:
        return SSTORE_RESET, SSTORE_CLEAR_REFUND
    return SSTORE_RESET, 0


def intrinsic_gas(tx_type: str, code_len: int = 0, input_len: int = 0) -> int:
    """计算交易内在费用。"""
    cost = INTRINSIC_TX
    if tx_type == "deploy":
        cost += DEPLOY_BASE + TX_CODE_BYTE_COST * code_len
    elif tx_type == "invoke":
        cost += TX_INPUT_WORD_COST * input_len
    return cost
