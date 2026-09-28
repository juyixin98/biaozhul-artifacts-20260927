"""唯一支持的操作码表。

本系统是*受限*栈脚本，**不**声称兼容任何完整链脚本系统。
下列操作码构成穷举白名单；脚本中出现任何其它字节都会在解析期被拒绝
（input.unknown_opcode / input.reserved_opcode）。

数值编号沿用经典栈脚本的编号惯例便于对照，但语义以本项目文档
(docs/opcodes.md) 为准，且仅实现下表中的行为。
"""

from __future__ import annotations

from dataclasses import dataclass

# 立即数推送
OP_0 = 0x00
OP_DATA_1 = 0x4C
OP_DATA_2 = 0x4D
OP_DATA_4 = 0x4E
OP_1NEGATE = 0x4F
OP_1 = 0x51
OP_2 = 0x52
OP_3 = 0x53
OP_4 = 0x54
OP_5 = 0x55
OP_6 = 0x56
OP_7 = 0x57
OP_8 = 0x58
OP_9 = 0x59
OP_10 = 0x5A
OP_11 = 0x5B
OP_12 = 0x5C
OP_13 = 0x5D
OP_14 = 0x5E
OP_15 = 0x5F
OP_16 = 0x60

# 流控
OP_NOP = 0x61
OP_IF = 0x63
OP_NOTIF = 0x64
OP_ELSE = 0x67
OP_ENDIF = 0x68
OP_VERIFY = 0x69
OP_RETURN = 0x6A

# 栈
OP_TOALTSTACK = 0x6B
OP_FROMALTSTACK = 0x6C
OP_DUP = 0x76
OP_DROP = 0x75
OP_SWAP = 0x7C
OP_DEPTH = 0x74

# 逻辑 / 比较
OP_EQUAL = 0x87
OP_EQUALVERIFY = 0x88

# 数值 / 位运算
OP_NOT = 0x91
OP_BOOLAND = 0x9A
OP_BOOLOR = 0x9B
OP_SIZE = 0x82

# 密码学
OP_RIPEMD160 = 0xA6
OP_SHA256 = 0xA8
OP_HASH160 = 0xA9
OP_HASH256 = 0xAA
OP_CHECKSIG = 0xAC
OP_CHECKSIGVERIFY = 0xAD
OP_CHECKMULTISIG = 0xAE
OP_CHECKMULTISIGVERIFY = 0xAF


@dataclass(frozen=True)
class OpSpec:
    value: int
    name: str
    desc: str


# 穷举白名单（立即数 0x01..0x4B 隐式支持，不在此表）
SUPPORTED: dict[int, OpSpec] = {
    s.value: s
    for s in [
        OpSpec(OP_0, "OP_0", "压入空字节串（脚本假值）"),
        OpSpec(OP_DATA_1, "OP_PUSHDATA1", "1 字节长度前缀 + 数据"),
        OpSpec(OP_DATA_2, "OP_PUSHDATA2", "2 字节长度前缀 + 数据"),
        OpSpec(OP_DATA_4, "OP_PUSHDATA4", "4 字节长度前缀 + 数据"),
        OpSpec(OP_1NEGATE, "OP_1NEGATE", "压入 -1"),
        OpSpec(OP_1, "OP_1", "压入 1"),
        *[OpSpec(0x51 + n - 1, f"OP_{n}", f"压入 {n}") for n in range(2, 17)],
        OpSpec(OP_NOP, "OP_NOP", "空操作"),
        OpSpec(OP_IF, "OP_IF", "栈顶为真则执行本分支"),
        OpSpec(OP_NOTIF, "OP_NOTIF", "栈顶为假则执行本分支"),
        OpSpec(OP_ELSE, "OP_ELSE", "分支取反"),
        OpSpec(OP_ENDIF, "OP_ENDIF", "分支结束"),
        OpSpec(OP_VERIFY, "OP_VERIFY", "栈顶为假则失败"),
        OpSpec(OP_RETURN, "OP_RETURN", "立即失败（受限环境）"),
        OpSpec(OP_TOALTSTACK, "OP_TOALTSTACK", "主栈 -> 副栈"),
        OpSpec(OP_FROMALTSTACK, "OP_FROMALTSTACK", "副栈 -> 主栈"),
        OpSpec(OP_DUP, "OP_DUP", "复制栈顶"),
        OpSpec(OP_DROP, "OP_DROP", "弹出栈顶"),
        OpSpec(OP_SWAP, "OP_SWAP", "交换栈顶两元素"),
        OpSpec(OP_DEPTH, "OP_DEPTH", "压入主栈当前深度"),
        OpSpec(OP_EQUAL, "OP_EQUAL", "两元素字节相等性"),
        OpSpec(OP_EQUALVERIFY, "OP_EQUALVERIFY", "相等且为真，否则失败"),
        OpSpec(OP_NOT, "OP_NOT", "逻辑非"),
        OpSpec(OP_BOOLAND, "OP_BOOLAND", "逻辑与"),
        OpSpec(OP_BOOLOR, "OP_BOOLOR", "逻辑或"),
        OpSpec(OP_SIZE, "OP_SIZE", "压入栈顶字节长度"),
        OpSpec(OP_RIPEMD160, "OP_RIPEMD160", "RIPEMD-160"),
        OpSpec(OP_SHA256, "OP_SHA256", "SHA-256"),
        OpSpec(OP_HASH160, "OP_HASH160", "RIPEMD160(SHA256(x))"),
        OpSpec(OP_HASH256, "OP_HASH256", "SHA256(SHA256(x))"),
        OpSpec(OP_CHECKSIG, "OP_CHECKSIG", "校验 ECDSA 签名"),
        OpSpec(OP_CHECKSIGVERIFY, "OP_CHECKSIGVERIFY", "CHECKSIG + VERIFY"),
        OpSpec(OP_CHECKMULTISIG, "OP_CHECKMULTISIG", "m-of-n 门槛验签（公钥/签名均不得重复计数）"),
        OpSpec(OP_CHECKMULTISIGVERIFY, "OP_CHECKMULTISIGVERIFY", "CHECKMULTISIG + VERIFY"),
    ]
}

# 分支类操作码
BRANCH_OPS = frozenset({OP_IF, OP_NOTIF, OP_ELSE, OP_ENDIF})

# 带立即数推送的范围
DIRECT_PUSH_MIN = 0x01
DIRECT_PUSH_MAX = 0x4B


def name_of(op: int) -> str:
    if DIRECT_PUSH_MIN <= op <= DIRECT_PUSH_MAX:
        return f"PUSH_{op}"
    spec = SUPPORTED.get(op)
    return spec.name if spec else f"UNKNOWN_0x{op:02X}"
