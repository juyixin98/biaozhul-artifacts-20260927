"""链状态内核：账户/nonce/余额/代码/存储 + 交易状态转换。

职责边界
--------
* 准入（验签、nonce、内在 gas、余额、目标存在性、代码可解码性）失败 →
  :class:`Rejected`：交易**不执行、不入块、不扣任何费用、nonce 不递增**；
* 一旦准入通过，nonce 立即递增，并按上限预扣 gas（托管）；
* VM 成功 → 写集提交，清零退款受上限后返还；
* VM REVERT → 状态写回滚，收取 intrinsic + 实际执行消耗，剩余返还；
* VM 异常中止（out_of_gas 等）→ 状态写回滚，**全部 gas 不退还**。

状态根
------
``state_root`` 是对 (balances, nonces, 代码摘要表, storage) 规范化后求 SHA-256，
纯函数、可跨进程复算；收据同时记录执行前/后状态根。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import crypto, gas as G
from .errors import Rejected
from .models import b64d, verify_envelope
from .opcodes import decode
from .version import ENGINE_VERSION
from .vm import VirtualMachine

ZERO_HASH = "0" * 64


@dataclass
class ChainState:
    nonces: dict[str, int] = field(default_factory=dict)
    balances: dict[str, int] = field(default_factory=dict)
    codes: dict[str, bytes] = field(default_factory=dict)
    storage: dict[tuple[str, int], int] = field(default_factory=dict)
    height: int = 0  # 已接受（含失败扣费）交易数，每笔一高度

    def state_root(self) -> str:
        storage_items = sorted(
            (addr, slot, value) for (addr, slot), value in self.storage.items()
        )
        code_hashes = sorted(
            (addr, crypto.sha256_hex(code)) for addr, code in self.codes.items()
        )
        payload = {
            "balances": sorted(self.balances.items()),
            "nonces": sorted(self.nonces.items()),
            "codes": code_hashes,
            "storage": storage_items,
        }
        return crypto.digest_payload(payload)


def _storage_items(writes: dict[tuple[str, int], int]) -> list[list[Any]]:
    return [[addr, slot, value] for (addr, slot), value in sorted(writes.items())]


class Receipt:
    def __init__(self, data: dict[str, Any]) -> None:
        data["result_digest"] = crypto.digest_payload(
            {k: v for k, v in data.items() if k != "result_digest"}
        )
        self.data = data

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def digest(self) -> str:
        return self.data["result_digest"]


class Kernel:
    """状态转换机。``diag`` 是可选诊断回调 (level, code, fields)。"""

    def __init__(self, state: ChainState | None = None, diag=None) -> None:
        self.state = state or ChainState()
        self.diag = diag

    def _log(self, level: str, code: str, **fields: Any) -> None:
        if self.diag is not None:
            safe = {k: v for k, v in fields.items()}
            self.diag(level, code, safe)

    # ---- 查询/夹具 ----
    def get_storage(self, address: str, slot: int) -> int:
        return self.state.storage.get((address, slot), 0)

    def get_balance(self, address: str) -> int:
        return self.state.balances.get(address, 0)

    def credit(self, address: str, amount: int) -> None:
        """本地合成注资（夹具/测试/初始化用）。"""
        self.state.balances[address] = self.state.balances.get(address, 0) + amount

    # ---- 主入口 ----
    def apply_tx(self, envelope: dict[str, Any]) -> Receipt:
        # 1) 验签 + 规范化（不产生副作用）
        try:
            body, tx_hash = verify_envelope(envelope)
        except Rejected as rej:
            self._log("warn", rej.code, reason=rej.message)
            raise
        sender = body["from"]
        pre_root = self.state.state_root()

        # 2) nonce
        expected_nonce = self.state.nonces.get(sender, 0)
        if body["nonce"] != expected_nonce:
            self._log("warn", "bad_nonce", tx=tx_hash[:16],
                      sender=sender, got=body["nonce"], expected=expected_nonce)
            raise Rejected("bad_nonce",
                           f"nonce {body['nonce']} != expected {expected_nonce}")

        # 3) 内在费用
        code_len = len(b64d(body["code_b64"])) if body["type"] == "deploy" else 0
        intrinsic = G.intrinsic_gas(body["type"], code_len=code_len,
                                    input_len=len(body["input"]))
        if body["gas_limit"] < intrinsic:
            self._log("warn", "gas_too_low", tx=tx_hash[:16],
                      gas_limit=body["gas_limit"], intrinsic=intrinsic)
            raise Rejected("gas_too_low",
                           f"gas_limit {body['gas_limit']} < intrinsic {intrinsic}")

        # 4) 余额（gas 以 1:1 本地合成代币计）
        balance = self.state.balances.get(sender, 0)
        if balance < body["gas_limit"]:
            self._log("warn", "insufficient_balance", tx=tx_hash[:16],
                      sender=sender, balance=balance, gas_limit=body["gas_limit"])
            raise Rejected("insufficient_balance",
                           f"balance {balance} < gas_limit {body['gas_limit']}")

        # 5) 类型相关的最终准入检查（仍在任何状态改动之前）
        if body["type"] == "deploy":
            code = b64d(body["code_b64"])
            try:
                decode(code)
            except ValueError as exc:
                self._log("warn", "invalid_bytecode", tx=tx_hash[:16], reason=str(exc))
                raise Rejected("invalid_bytecode", str(exc)) from None
        else:
            if body["to"] not in self.state.codes:
                self._log("warn", "contract_not_found", tx=tx_hash[:16], to=body["to"])
                raise Rejected("contract_not_found", f"no code at {body['to']}")

        # ---- 准入通过：nonce 递增 + gas 全额托管 ----
        self.state.balances[sender] = balance - body["gas_limit"]
        self.state.nonces[sender] = expected_nonce + 1

        if body["type"] == "deploy":
            receipt = self._apply_deploy(body, tx_hash, intrinsic, code)
        else:
            receipt = self._apply_invoke(body, tx_hash, intrinsic)

        self.state.height += 1
        receipt.data["pre_state_root"] = pre_root
        receipt.data["post_state_root"] = self.state.state_root()
        receipt.data["height"] = self.state.height
        # 状态根/高度是最后落定的字段，需要重算结果摘要
        receipt.data["result_digest"] = crypto.digest_payload(
            {k: v for k, v in receipt.data.items() if k != "result_digest"}
        )
        self._log("info", "accepted",
                  tx=tx_hash[:16], status=receipt.data["status"],
                  gas_charged=receipt.data["gas_charged"],
                  halt=receipt.data.get("halt_code"))
        return receipt

    # ---- 部署 ----
    def _apply_deploy(self, body: dict, tx_hash: str, intrinsic: int,
                      code: bytes) -> Receipt:
        # CREATE 风格地址：sender + nonce 派生（绑定输入）
        seed = f"{body['from']}:{body['nonce']}".encode()
        address = "0x" + crypto.sha256_hex(seed)[:16]
        self.state.codes[address] = code
        # 教学链部署不运行初始化函数：固定收 intrinsic，未用 gas 全部返还
        return self._build_receipt(
            body, tx_hash, intrinsic, vm_used=0, refund=0,
            status=1, halt_code=None, reverted=False,
            output=[], writes={}, to=address, deployed_address=address,
        )

    # ---- 调用 ----
    def _apply_invoke(self, body: dict, tx_hash: str, intrinsic: int) -> Receipt:
        to = body["to"]
        vm = VirtualMachine(self.state.codes)
        result = vm.execute(
            address=to,
            code=self.state.codes[to],
            calldata=body["input"],
            gas_limit=body["gas_limit"] - intrinsic,
            committed=self.state.storage,
        )
        if result.status == 1:
            for key, value in result.writes.items():
                self.state.storage[key] = value
        refund = result.gas_refund if result.status == 1 else 0
        return self._build_receipt(
            body, tx_hash, intrinsic,
            vm_used=result.gas_used,
            refund=refund,
            status=result.status,
            halt_code=result.halt_code,
            reverted=result.reverted,
            output=result.output,
            writes=result.writes if result.status == 1 else {},
            to=to,
            deployed_address=None,
        )

    # ---- 结算与收据 ----
    def _build_receipt(self, body, tx_hash, intrinsic, *, vm_used, refund,
                       status, halt_code, reverted, output, writes, to,
                       deployed_address) -> Receipt:
        consumed = intrinsic + vm_used
        if status == 1 and refund and vm_used:
            refund = min(refund, vm_used // G.REFUND_FACTOR_DENOM)
        else:
            refund = 0
        gas_charged = consumed - refund
        returned = body["gas_limit"] - gas_charged
        if returned > 0:
            self.state.balances[body["from"]] = (
                self.state.balances.get(body["from"], 0) + returned
            )

        data = {
            "tx_hash": tx_hash,
            "type": body["type"],
            "from": body["from"],
            "to": to,
            "deployed_address": deployed_address,
            "status": status,
            "reverted": reverted,
            "halt_code": halt_code,
            "output": output,
            "nonce": body["nonce"],
            "gas_limit": body["gas_limit"],
            "intrinsic_gas": intrinsic,
            "gas_exec_used": vm_used,
            "gas_refund": refund,
            "gas_charged": gas_charged,
            "writes": _storage_items(writes),
            "pre_state_root": ZERO_HASH,
            "post_state_root": ZERO_HASH,
            "height": self.state.height,
            "engine_version": ENGINE_VERSION,
            "input_digest": tx_hash,
        }
        return Receipt(data)
