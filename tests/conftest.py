"""共享测试夹具：全部为本地合成数据。

**关键约定（对应验收要求“参考答案不能全部由被测核心实现自身生成”）**：

* 算术 / gas 的期望值全部以整数字面量手写，并用本文件里独立实现的
  纯 Python 参考函数 ``reference_*`` 交叉验证（这些参考函数不导入
  任何 ``teaching_chain`` 代码）；
* 确定性测试在独立进程（``multiprocessing``）里重放同一批交易，
  比对收据哈希。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from teaching_chain.encoding import KeyPair, sign_transaction
from teaching_chain.vm import gas as gas_schedule
from teaching_chain.vm.assembler import assemble

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# 合成密钥（固定种子，跨进程一致；绝非真实密钥）
# ---------------------------------------------------------------------------
def _kp(index: int) -> KeyPair:
    seed = hashlib.sha256(f"teaching-chain-fixture-{index}".encode()).digest()
    return KeyPair.from_seed(seed)


@pytest.fixture(scope="session")
def alice() -> KeyPair:
    return _kp(0)


@pytest.fixture(scope="session")
def bob() -> KeyPair:
    return _kp(1)


def make_tx(keypair: KeyPair, code_hex: str, *, chain: str = "teaching-chain-local",
            nonce: int = 0, gas_limit: int = 100_000, trace: bool = False) -> dict:
    """构造一笔已签名交易（待签内容固定为 chain/nonce/code/gas_limit）。"""
    body = {"chain": chain, "nonce": nonce, "code": code_hex, "gas_limit": gas_limit}
    signed = sign_transaction(keypair, body)
    signed["pubkey"] = keypair.public_bytes().hex()
    if trace:
        signed["trace"] = True
    return signed


def asm(text: str) -> str:
    return assemble(text).hex()


# ---------------------------------------------------------------------------
# 独立参考实现（不依赖被测包的 VM）
# ---------------------------------------------------------------------------
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1


def reference_check_i64(value: int) -> bool:
    return I64_MIN <= value <= I64_MAX


def reference_intrinsic(code: bytes) -> int:
    return 21 + sum(1 if b == 0 else 4 for b in code)


def reference_memory_cost(prev_words: int, new_words: int) -> int:
    def total(w: int) -> int:
        return 3 * w + w * w // 512
    return total(new_words) - total(prev_words)


def reference_add(a: int, b: int) -> int | str:
    """返回结果；越界返回类别名。"""
    r = a + b
    return r if reference_check_i64(r) else "INTEGER_OVERFLOW"


def reference_div(a: int, b: int) -> int | str:
    if b == 0:
        return "DIV_BY_ZERO"
    q = abs(a) // abs(b)
    q = -q if (a < 0) ^ (b < 0) else q
    return q if reference_check_i64(q) else "INTEGER_OVERFLOW"


def reference_truncating_divmod(a: int, b: int) -> tuple[int, int] | str:
    if b == 0:
        return "DIV_BY_ZERO"
    q = abs(a) // abs(b)
    q = -q if (a < 0) ^ (b < 0) else q
    r = abs(a) % abs(b)
    r = -r if a < 0 else r
    return q, r


# ---------------------------------------------------------------------------
# 手写 gas 常量（与 gas 表解耦：若价格表被改，相关测试会失败，逼使显式复核）
# ---------------------------------------------------------------------------
EXPECTED_FEE = {
    "STOP": 0,
    "ADD": 3,
    "DIV": 5,
    "MOD": 5,
    "ADDMOD": 5,
    "SLOAD": 5,
    "SSTORE": 20,
    "CALL_BASE": 10,
    "PUSH": 3,
    "POP": 2,
}
EXPECTED_TX_MINIMUM = 21
EXPECTED_ZERO_BYTE = 1
EXPECTED_NONZERO_BYTE = 4


# 让未使用的导入在静态检查下保持“夹具库”性质
_ = gas_schedule
