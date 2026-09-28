"""交易线格式与规范化序列化（模块一：编码）。

交易 JSON 形态见 fixtures/README。注意：witness（解锁脚本/签名）**不**参与
规范化序列化，否则签名会覆盖自身；签名绑定的是 inputs 的 outpoint + value、
outputs、version、locktime 以及 domain（在 crypto.build_message 中注入）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import MALFORMED_TX, VerificationFailure


@dataclass(frozen=True)
class Outpoint:
    txid: str  # 64 hex
    index: int


@dataclass(frozen=True)
class TxInput:
    outpoint: Outpoint
    value: int  # 输入金额（本地测试链显式携带，内核校验与 UTXO 一致）
    witness_script: bytes  # 解锁脚本（推送签名等），不参与摘要


@dataclass(frozen=True)
class TxOutput:
    value: int
    pubkey_script: bytes  # 锁定脚本


@dataclass
class Transaction:
    version: int
    domain: str
    inputs: list[TxInput]
    outputs: list[TxOutput]
    locktime: int = 0

    def txid(self) -> bytes:
        from .crypto import hash256

        return hash256(self.canonical())

    def canonical(self) -> bytes:
        """确定性字节序列化：LEB128 长度前缀，字段固定序。"""
        buf = bytearray()
        buf += _u32(self.version)
        _leb(buf, len(self.domain.encode()))
        buf += self.domain.encode()
        buf += _u32(self.locktime)

        _leb(buf, len(self.inputs))
        for inp in self.inputs:
            txid_b = _hex32(inp.outpoint.txid)
            buf += txid_b
            buf += _u32(inp.outpoint.index)
            buf += _u64(inp.value)
            # witness 刻意不写入

        _leb(buf, len(self.outputs))
        for out in self.outputs:
            buf += _u64(out.value)
            _leb(buf, len(out.pubkey_script))
            buf += out.pubkey_script
        return bytes(buf)


def _u32(n: int) -> bytes:
    if not 0 <= n <= 0xFFFFFFFF:
        raise VerificationFailure(MALFORMED_TX, f"u32 out of range: {n}")
    return n.to_bytes(4, "little")


def _u64(n: int) -> bytes:
    if not 0 <= n <= 0xFFFFFFFFFFFFFFFF:
        raise VerificationFailure(MALFORMED_TX, f"u64 out of range: {n}")
    return n.to_bytes(8, "little")


def _leb(buf: bytearray, n: int) -> None:
    if n < 0:
        raise VerificationFailure(MALFORMED_TX, f"negative length: {n}")
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def _hex32(s: str) -> bytes:
    try:
        b = bytes.fromhex(s)
    except ValueError as exc:
        raise VerificationFailure(MALFORMED_TX, f"bad hex txid: {s!r}") from exc
    if len(b) != 32:
        raise VerificationFailure(MALFORMED_TX, f"txid must be 32B, got {len(b)}")
    return b


def transaction_from_dict(obj: dict, chain) -> Transaction:
    """宽松字典 -> 强类型 Transaction，结构问题统一归类 input.malformed_tx。

    chain: ChainConfig（提供输入/输出数量与金额上限）。
    """
    if not isinstance(obj, dict):
        raise VerificationFailure(MALFORMED_TX, "tx must be object")
    try:
        version = int(obj.get("version", 1))
        locktime = int(obj.get("locktime", 0))
        domain = obj.get("domain")
        if not isinstance(domain, str) or not domain:
            from ..errors import DOMAIN_MISSING

            raise VerificationFailure(DOMAIN_MISSING, "tx.domain required")

        raw_ins = obj.get("inputs")
        raw_outs = obj.get("outputs")
        if not isinstance(raw_ins, list) or not isinstance(raw_outs, list):
            raise VerificationFailure(MALFORMED_TX, "inputs/outputs must be arrays")
        if not 1 <= len(raw_ins) <= chain.max_inputs:
            raise VerificationFailure(MALFORMED_TX, f"inputs count not in 1..{chain.max_inputs}")
        if not 1 <= len(raw_outs) <= chain.max_outputs:
            raise VerificationFailure(MALFORMED_TX, f"outputs count not in 1..{chain.max_outputs}")

        inputs: list[TxInput] = []
        for ri in raw_ins:
            op = ri["outpoint"]
            inputs.append(
                TxInput(
                    outpoint=Outpoint(txid=str(op["txid"]), index=int(op["index"])),
                    value=int(ri["value"]),
                    witness_script=bytes.fromhex(ri["witness_script"]),
                )
            )

        outputs: list[TxOutput] = []
        for ro in raw_outs:
            value = int(ro["value"])
            if not 0 <= value <= chain.max_value:
                raise VerificationFailure(MALFORMED_TX, f"output value out of range: {value}")
            outputs.append(
                TxOutput(value=value, pubkey_script=bytes.fromhex(ro["pubkey_script"]))
            )
    except VerificationFailure:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise VerificationFailure(MALFORMED_TX, f"missing/typed field: {exc}") from exc

    return Transaction(version=version, domain=domain, inputs=inputs, outputs=outputs,
                       locktime=locktime)
