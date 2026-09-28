"""链状态内核：交易校验、确定性执行、收据与区块。

内核**不接触**网络、时间、随机数与文件系统。它的全部输出只依赖：

* 固定的程序版本（``config.PROGRAM_VERSION``）；
* 父区块哈希与单调区块号；
* 交易内容（含签名）及由签名确定的调用者。

收据中包含输入摘要（``input_digest``：对不含签名的交易内容的规范哈希）
与程序版本，因此“同样的输入 + 同样的版本”才能复现同样的收据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import encoding
from .config import PROGRAM_VERSION, RECEIPT_VERSION
from .vm import Failure, execute
from .vm import gas as gas_schedule

GENESIS_PARENT = "00" * 32
SEQUENCE_NOBLOCK = -1  # 顺序化序号（不绑定区块时，例如只读预演）


class TransactionError(ValueError):
    """交易静态校验失败（形状 / 签名 / gas）。该类错误不产生收据、不消耗 gas。"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


# 收据字段固定顺序，所有序列化经规范编码完成
RECEIPT_FIELDS = (
    "receipt_version",
    "program_version",
    "chain",
    "block_number",
    "tx_index",
    "tx_hash",
    "input_digest",
    "caller",
    "intrinsic_gas",
    "gas_limit",
    "status",
    "gas_used",
    "error_category",
    "error_pc",
    "return_value",
    "state_root",
    "trace",
)


@dataclass(frozen=True)
class Receipt:
    receipt_version: int
    program_version: str
    chain: str
    block_number: int
    tx_index: int
    tx_hash: str
    input_digest: str
    caller: str
    intrinsic_gas: int
    gas_limit: int
    status: int                     # 1 成功 / 0 失败（执行失败仍产生收据并保留费用）
    gas_used: int
    error_category: str | None
    error_pc: int
    return_value: int
    state_root: str
    trace: tuple[str, ...]

    def to_dict(self, include_trace: bool = True) -> dict[str, Any]:
        data = {k: getattr(self, k) for k in RECEIPT_FIELDS}
        if not include_trace:
            data["trace"] = []
        return data

    def canonical_bytes(self) -> bytes:
        return encoding.encode(self.to_dict(include_trace=True))

    def digest(self) -> str:
        return hashlib_sha256_hex(self.canonical_bytes())


def hashlib_sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def normalize_transaction(raw: Any) -> dict[str, Any]:
    """把外部 JSON 交易规整为内部形态，并做形状校验（不验签、不执行）。"""
    if not isinstance(raw, dict):
        raise TransactionError("TX_NOT_OBJECT", "交易必须是 JSON 对象")
    required = ("chain", "nonce", "gas_limit", "pubkey", "signature", "signer")
    missing = [k for k in required if k not in raw]
    if "code" not in raw and "code_hex" not in raw:
        missing.append("code")
    if missing:
        raise TransactionError("TX_MISSING_FIELDS", f"缺少字段: {','.join(missing)}")

    chain = raw["chain"]
    if not isinstance(chain, str) or not chain:
        raise TransactionError("TX_BAD_CHAIN", "chain 必须是非空字符串")
    nonce = raw["nonce"]
    if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce < 0:
        raise TransactionError("TX_BAD_NONCE", "nonce 必须是非负整数")
    code_hex = raw.get("code", raw.get("code_hex"))
    if not isinstance(code_hex, str) or not _is_hex(code_hex):
        raise TransactionError("TX_BAD_CODE", "code 必须是偶数长度十六进制字符串")
    try:
        code = bytes.fromhex(code_hex[2:] if code_hex.startswith("0x") else code_hex)
    except ValueError as exc:
        raise TransactionError("TX_BAD_CODE", "code 不是合法十六进制") from exc
    gas_limit = raw["gas_limit"]
    if not isinstance(gas_limit, int) or isinstance(gas_limit, bool) or gas_limit <= 0:
        raise TransactionError("TX_BAD_GAS", "gas_limit 必须是正整数")
    if gas_limit > 1_000_000:
        raise TransactionError("TX_BAD_GAS", "gas_limit 超过教学上限 1,000,000")
    trace = bool(raw.get("trace", False))

    tx = {
        "chain": chain,
        "nonce": nonce,
        "code_hex": code.hex(),
        "gas_limit": gas_limit,
        "pubkey": raw["pubkey"],
        "signature": raw["signature"],
        "signer": raw["signer"],
        "trace": trace,
    }
    return tx


def _is_hex(value: str) -> bool:
    body = value[2:] if value.startswith("0x") else value
    if len(body) % 2:
        return False
    try:
        int(body, 16)
        return True
    except ValueError:
        return False


def transaction_to_external(transaction: dict[str, Any]) -> dict[str, Any]:
    """把内部规整交易转回外部规范字段（持久化 / 重放使用 ``code``）。"""
    return {
        "chain": transaction["chain"],
        "nonce": transaction["nonce"],
        "code": transaction["code_hex"],
        "gas_limit": transaction["gas_limit"],
        "pubkey": transaction["pubkey"],
        "signature": transaction["signature"],
        "signer": transaction["signer"],
        **({"trace": True} if transaction.get("trace") else {}),
    }


def verify_tx(transaction: dict[str, Any], expected_chain: str) -> bytes:
    """验签 + 链号检查，返回字节码。"""
    if transaction["chain"] != expected_chain:
        raise TransactionError(
            "TX_CHAIN_MISMATCH",
            f"交易链号 {transaction['chain']!r} 与节点链 {expected_chain!r} 不符",
        )
    try:
        caller = encoding.verify_transaction(
            {
                "chain": transaction["chain"],
                "nonce": transaction["nonce"],
                "code": transaction["code_hex"],
                "gas_limit": transaction["gas_limit"],
                "pubkey": transaction["pubkey"],
                "signature": transaction["signature"],
                "signer": transaction["signer"],
            }
        )
    except encoding.SignatureError as exc:
        raise TransactionError("TX_BAD_SIGNATURE", str(exc)) from exc
    return bytes.fromhex(transaction["code_hex"])


def tx_hash(transaction: dict[str, Any]) -> str:
    """完整交易哈希：对**含签名**的规范交易取哈希。"""
    full = {
        "chain": transaction["chain"],
        "nonce": transaction["nonce"],
        "code": transaction["code_hex"],
        "gas_limit": transaction["gas_limit"],
        "pubkey": transaction["pubkey"],
        "signature": transaction["signature"],
        "signer": transaction["signer"],
    }
    return encoding.hexhash(full)


def state_root(storage: dict[int, int]) -> str:
    """账户 KV 存储的规范状态根：排序键后的 {key: value} 字典哈希。"""
    return encoding.hexhash({str(k): storage[k] for k in sorted(storage)})


@dataclass
class ProcessedTransaction:
    receipt: Receipt
    storage_after: dict[int, int]
    code: bytes = b""
    caller: str = ""


def process_transaction(
    transaction: dict[str, Any],
    storage_before: dict[int, int],
    chain: str,
    block_number: int,
    tx_index: int,
    *,
    record_trace: bool | None = None,
) -> ProcessedTransaction:
    """校验并执行一笔已规整的交易，生成收据。

    静态校验失败（``TransactionError``）由调用方决定如何应答；
    执行失败（如越界、REVERT、OOG）属于共识结果：状态回滚、费用保留、
    产出 status=0 的收据。
    """
    code = verify_tx(transaction, chain)
    caller = transaction["signer"].lower()
    intrinsic = gas_schedule.intrinsic_gas(code)
    gas_limit = transaction["gas_limit"]
    record = transaction["trace"] if record_trace is None else record_trace

    input_digest = encoding.transaction_digest(
        {
            "chain": transaction["chain"],
            "nonce": transaction["nonce"],
            "code": transaction["code_hex"],
            "gas_limit": transaction["gas_limit"],
            "pubkey": transaction["pubkey"],
        }
    )
    full_tx_hash = tx_hash(transaction)

    if intrinsic > gas_limit:
        receipt = Receipt(
            receipt_version=RECEIPT_VERSION,
            program_version=PROGRAM_VERSION,
            chain=chain,
            block_number=block_number,
            tx_index=tx_index,
            tx_hash=full_tx_hash,
            input_digest=input_digest,
            caller=caller,
            intrinsic_gas=intrinsic,
            gas_limit=gas_limit,
            status=0,
            gas_used=gas_limit,
            error_category=str(Failure.OUT_OF_GAS),
            error_pc=-1,
            return_value=0,
            state_root=state_root(storage_before),
            trace=(f"intrinsic gas {intrinsic} 超过 gas_limit {gas_limit}，"
                   "拒绝执行，按规则保留全部 gas_limit",),
        )
        return ProcessedTransaction(receipt, dict(storage_before), code, caller)

    exec_gas = gas_limit - intrinsic
    trace_steps: list[str] = [f"intrinsic_gas={intrinsic}，执行 gas={exec_gas}"]
    result = execute(code, exec_gas, storage_before, trace=record)
    if record:
        trace_steps.extend(result.trace)

    receipt = Receipt(
        receipt_version=RECEIPT_VERSION,
        program_version=PROGRAM_VERSION,
        chain=chain,
        block_number=block_number,
        tx_index=tx_index,
        tx_hash=full_tx_hash,
        input_digest=input_digest,
        caller=caller,
        intrinsic_gas=intrinsic,
        gas_limit=gas_limit,
        status=int(result.ok),
        gas_used=intrinsic + result.gas_used,
        error_category=result.error_category,
        error_pc=result.error_pc,
        return_value=result.return_value,
        state_root=state_root(result.storage),
        trace=tuple(trace_steps if record else ()),
    )
    return ProcessedTransaction(receipt, result.storage, code, caller)


@dataclass
class Block:
    chain: str
    number: int
    parent_hash: str
    transactions: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    receipts: tuple[Receipt, ...] = field(default_factory=tuple)
    state_root: str = ""

    def header_dict(self) -> dict[str, Any]:
        return {
            "chain": self.chain,
            "number": self.number,
            "parent_hash": self.parent_hash,
            "tx_count": len(self.transactions),
            # 收据摘要绑定全部执行结果；状态根绑定最终 KV 状态
            "receipts_root": encoding.hexhash([r.digest() for r in self.receipts]),
            "state_root": self.state_root,
            "program_version": PROGRAM_VERSION,
        }

    def hash(self) -> str:
        return encoding.hexhash(self.header_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.header_dict(),
            "hash": self.hash(),
            "transactions": list(self.transactions),
            "receipts": [r.to_dict() for r in self.receipts],
        }


@dataclass
class ChainState:
    """纯内存的链状态：单账户共享 KV、区块序列。"""

    chain: str = "teaching-chain-local"
    storage: dict[int, int] = field(default_factory=dict)
    height: int = -1
    head_hash: str = GENESIS_PARENT
    blocks: list[Block] = field(default_factory=list)
    _used_tx_hashes: set[str] = field(default_factory=set)

    def state_root(self) -> str:
        return state_root(self.storage)

    def apply_block(self, raw_transactions: list[dict[str, Any]]) -> tuple[Block, list[ProcessedTransaction]]:
        """顺序执行一批交易并封块。

        任何一笔静态校验失败（签名 / 形状）都会**阻止封块**并抛出
        ``TransactionError``——教学链不包含“跳过坏交易”的复杂内存池逻辑；
        执行失败的交易正常入块（status=0，状态回滚、费用保留）。
        """
        normalized: list[dict[str, Any]] = []
        for raw in raw_transactions:
            tx = normalize_transaction(raw)
            h = tx_hash(tx)
            if h in self._used_tx_hashes:
                raise TransactionError("TX_DUPLICATE", f"重复交易 {h[:16]}…")
            normalized.append(tx)

        processed: list[ProcessedTransaction] = []
        working_storage = dict(self.storage)
        block_number = self.height + 1
        for idx, tx in enumerate(normalized):
            p = process_transaction(tx, working_storage, self.chain, block_number, idx)
            processed.append(p)
            working_storage = p.storage_after

        receipts = tuple(p.receipt for p in processed)
        block = Block(
            chain=self.chain,
            number=block_number,
            parent_hash=self.head_hash,
            transactions=tuple(normalized),
            receipts=receipts,
            state_root=state_root(working_storage),
        )
        self.storage = working_storage
        self.height = block_number
        self.head_hash = block.hash()
        self.blocks.append(block)
        self._used_tx_hashes.update(tx_hash(t) for t in normalized)
        return block, processed

    def dry_run(self, raw_transaction: dict[str, Any]) -> ProcessedTransaction:
        """只读预演：验签 + 执行，但不改变链状态、不查重、不封块。"""
        tx = normalize_transaction(raw_transaction)
        return process_transaction(
            tx, self.storage, self.chain, SEQUENCE_NOBLOCK, 0, record_trace=True
        )
