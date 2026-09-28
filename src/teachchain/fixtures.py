"""本地合成夹具：确定性密钥、账户、示例合约与信封构造。

密钥由固定助记种子经 SHA-256 派生（**仅用于本地教学/测试**），因此：

* 不访问宿主随机源，任何机器上 ``fixtures.signer("alice")`` 得到同一账户；
* 与生产密钥体系无关；README 明确标注这些地址永不持有真实价值。
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from . import crypto
from .models import b64e
from .opcodes import assemble

_FIXTURE_SEED = b"teachchain/local-synthetic-fixture/v1:"


def signer(name: str) -> crypto.Signer:
    """由固定名字派生 32 字节 Ed25519 种子，再构造签名器。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    seed = hashlib.sha256(_FIXTURE_SEED + name.encode()).digest()
    return crypto.Signer(Ed25519PrivateKey.from_private_bytes(seed))


def address_of(name: str) -> str:
    return signer(name).address


from cryptography.hazmat.primitives import serialization


def _pub_b64(s: crypto.Signer) -> str:
    raw = s.key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode()


def envelope(s: crypto.Signer, tx_type: str, *, nonce: int, gas_limit: int,
             to: str | None = None, code: bytes | None = None,
             words: list[int] | None = None) -> dict[str, Any]:
    """构造并签名一个交易信封。"""
    body: dict[str, Any] = {
        "type": tx_type,
        "from": s.address,
        "nonce": nonce,
        "gas_limit": gas_limit,
        "to": to,
        "code_b64": b64e(code) if code is not None else None,
        "input": list(words or []),
    }
    digest = crypto.digest_payload(body)
    return {
        "tx": body,
        "pub_b64": _pub_b64(s),
        "sig_b64": s.sign_digest_b64(digest),
    }


# ---- 示例合约（教学汇编） ----

COUNTER_ASM = """
# counter：calldata[0] 为增量；存储槽 1 保存计数。
# SSTORE 弹栈 (slot=栈顶, value)：先压 value(n+old)，再压 slot(1)。
PUSH 0
CALLDATALOAD        # n
PUSH 0
SLOAD               # n, old（栈顶 old）
ADD                 # n+old（value）
PUSH 1              # value, slot(=1)（slot 在栈顶）
SSTORE
PUSH 1
PUSH 0
MSTORE              # mem[0]=1
PUSH 1
PUSH 0
RETURN
"""

# 写后异常：先 SSTORE 槽 7，再用 INVALID 异常中止 -> 写必须回滚、gas 不退还
WRITE_THEN_HALT_ASM = """
PUSH 42             # value
PUSH 7              # slot（栈顶）
SSTORE
INVALID
"""

# 先 SSTORE 槽 8 再 REVERT -> REVERT 时写回滚，剩余 gas 退还
WRITE_THEN_REVERT_ASM = """
PUSH 7              # value
PUSH 8              # slot
SSTORE
PUSH 1
PUSH 0
MSTORE
PUSH 1
PUSH 0
REVERT
"""

# 调用者：调用给定地址（由 calldata[0] 给出地址字），再写自己的槽 3。
# CALL 弹栈顺序（栈顶起）：gas, slot, addr —— 因此按 addr, slot, gas 的
# 逆序压栈。子调用失败(status=0)时父帧继续：槽 3 仍写入，演示“只回滚子帧范围”。
CALLER_ASM = """
PUSH 0
CALLDATALOAD        # 子地址（最先压）
PUSH 0              # slot 保留参数
PUSH 100000         # gas（栈顶）
CALL                # -> status, ret0
POP                 # 丢弃 ret0
PUSH 99
PUSH 3
SSTORE              # 无论子帧成败都写槽 3
PUSH 1
PUSH 0
MSTORE
PUSH 1
PUSH 0
RETURN
"""


def counter_code() -> bytes:
    return assemble(COUNTER_ASM)


def write_then_halt_code() -> bytes:
    return assemble(WRITE_THEN_HALT_ASM)


def write_then_revert_code() -> bytes:
    return assemble(WRITE_THEN_REVERT_ASM)


def caller_code() -> bytes:
    return assemble(CALLER_ASM)
