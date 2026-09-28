"""受限栈脚本验证内核。

模块划分：
- config:     独立配置加载（TOML + 环境变量覆盖）
- errors:     失败分类（输入/资源/计算/状态，四类可区分）
- opcodes:    唯一支持的操作码白名单
- script:     脚本/元素编解码、ScriptNum、脚本组装
- hashes:     RIPEMD160/SHA1/SHA256/HASH160/HASH256
- crypto:     成熟密码库（cryptography）SECP256K1 ECDSA 验签
- sighash:    绑定域标签的交易摘要
- transaction:交易数据结构与 txid
- vm:         受限栈虚拟机（资源限额、M-of-N 去重计数）
- runlog:     运行编号与可重放的结构化追踪日志
"""
from .errors import FailCode, FailKind, VmFailure, failure_result
from .vm import Machine, TraceEvent, VMResult
from .transaction import TxInput, TxOutput, Transaction, txid_of
from .sighash import signature_digest
from .script import assemble, disassemble, parse
from .config import Settings, load_settings

__all__ = [
    "FailCode",
    "FailKind",
    "VmFailure",
    "failure_result",
    "Machine",
    "TraceEvent",
    "VMResult",
    "TxInput",
    "TxOutput",
    "Transaction",
    "txid_of",
    "signature_digest",
    "assemble",
    "disassemble",
    "parse",
    "Settings",
    "load_settings",
]
