"""交易数据结构（本地测试协议 v1）。

- 输入：引用上一笔交易的 (txid, vout) 并携带 unlock（解锁脚本，hex）。
- 输出：金额（非负整数）+ lock（锁定脚本，hex）。
- txid = HASH256(canonical_json(交易线体))，与签名摘要使用同一份规范化编码，
  但 txid 文档是完整交易（含 unlock），且不带域标签。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import hashes as H
from .errors import FailCode, VmFailure


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class TxInput:
    txid: str   # 被花费输出所在交易的 txid（64 hex）
    vout: int
    unlock: str  # 解锁脚本 hex

    def wire(self) -> dict:
        return {"txid": self.txid, "vout": self.vout, "unlock": self.unlock}


@dataclass(frozen=True)
class TxOutput:
    value: int
    script: str  # 锁定脚本 hex

    def wire(self) -> dict:
        return {"value": self.value, "script": self.script}


@dataclass(frozen=True)
class Transaction:
    version: int = 1
    locktime: int = 0
    inputs: tuple[TxInput, ...] = field(default_factory=tuple)
    outputs: tuple[TxOutput, ...] = field(default_factory=tuple)

    def wire(self) -> dict:
        return {
            "version": self.version,
            "locktime": self.locktime,
            "inputs": [i.wire() for i in self.inputs],
            "outputs": [o.wire() for o in self.outputs],
        }

    def to_json(self) -> str:
        return canonical_json(self.wire())


def _is_hex32(s: str) -> bool:
    try:
        int(s, 16)
    except (TypeError, ValueError):
        return False
    return len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower())


def transaction_from_dict(obj: dict, *, allow_empty_inputs: bool = False) -> Transaction:
    """从 JSON 兼容字典构造并做结构性校验。结构错误 → TX_MALFORMED。

    allow_empty_inputs=True 仅供零输入铸币（genesis）使用；普通交易必须有输入。
    """
    if not isinstance(obj, dict):
        raise VmFailure(FailCode.REQUEST_MALFORMED, "请求体必须是 JSON 对象")
    try:
        version = int(obj["version"])
        locktime = int(obj["locktime"])
        raw_in = obj["inputs"]
        raw_out = obj["outputs"]
    except (KeyError, TypeError, ValueError) as exc:
        raise VmFailure(FailCode.REQUEST_MALFORMED, f"缺少/非法字段: {exc}") from None

    if version != 1:
        raise VmFailure(FailCode.TX_MALFORMED, f"仅支持 version=1，收到 {version}")
    if locktime < 0 or not isinstance(locktime, int):
        raise VmFailure(FailCode.TX_MALFORMED, "locktime 必须为非负整数")
    if not isinstance(raw_in, list) or (not raw_in and not allow_empty_inputs):
        raise VmFailure(FailCode.TX_MALFORMED, "inputs 必须为非空数组")
    if not isinstance(raw_out, list) or not raw_out:
        raise VmFailure(FailCode.TX_MALFORMED, "outputs 必须为非空数组")

    inputs = []
    for idx, item in enumerate(raw_in):
        if not isinstance(item, dict):
            raise VmFailure(FailCode.TX_MALFORMED, f"inputs[{idx}] 必须是对象")
        try:
            txid = str(item["txid"]).lower()
            vout = int(item["vout"])
            unlock = str(item.get("unlock", "")).lower()
        except (KeyError, TypeError, ValueError):
            raise VmFailure(FailCode.TX_MALFORMED, f"inputs[{idx}] 字段非法") from None
        if not _is_hex32(txid):
            raise VmFailure(FailCode.TX_MALFORMED, f"inputs[{idx}].txid 必须为 64 位小写 hex")
        if vout < 0:
            raise VmFailure(FailCode.TX_MALFORMED, f"inputs[{idx}].vout 不能为负")
        _hex_decode(unlock, f"inputs[{idx}].unlock")
        inputs.append(TxInput(txid=txid, vout=vout, unlock=unlock))

    outputs = []
    for idx, item in enumerate(raw_out):
        if not isinstance(item, dict):
            raise VmFailure(FailCode.TX_MALFORMED, f"outputs[{idx}] 必须是对象")
        try:
            value = int(item["value"])
            script = str(item["script"]).lower()
        except (KeyError, TypeError, ValueError):
            raise VmFailure(FailCode.TX_MALFORMED, f"outputs[{idx}] 字段非法") from None
        if value < 0:
            raise VmFailure(FailCode.TX_MALFORMED, f"outputs[{idx}].value 不能为负")
        _hex_decode(script, f"outputs[{idx}].script")
        outputs.append(TxOutput(value=value, script=script))

    return Transaction(version=version, locktime=locktime,
                       inputs=tuple(inputs), outputs=tuple(outputs))


def _hex_decode(s: str, field_name: str) -> bytes:
    try:
        return bytes.fromhex(s)
    except ValueError:
        raise VmFailure(FailCode.TX_MALFORMED, f"{field_name} 必须为偶数位 hex") from None


def txid_of(tx: Transaction) -> str:
    return H.hash256(tx.to_json().encode("utf-8")).hex()
