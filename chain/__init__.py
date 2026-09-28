"""链状态内核：UTXO 模型下的交易验证与提交边界。

规则（本地测试协议）：
- 普通交易：每个输入必须引用存在且未花费的 UTXO；输入金额合计必须**等于**
  输出金额合计（无费模型）→ 否则 VALUE_IMBALANCE。
- 验证先逐输入执行栈脚本；任何输入失败 → 整笔交易失败，**不做任何状态改动**。
- 仅全部通过时才在单个 SQLite 事务内：删除被花费 UTXO、插入新 UTXO、
  登记交易、追加链式写前日志。
- 零输入交易只能来自 genesis 引导（Store.bootstrap_genesis），服务接口不接受。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from stackvm.config import Settings
from stackvm.errors import FailCode, VmFailure
from stackvm.sighash import signature_digest
from stackvm.transaction import Transaction, txid_of
from stackvm.vm import VMResult, run_scripts


@dataclass
class InputEval:
    index: int
    prev_txid: str
    prev_vout: int
    prev_script: str
    value: int
    result: VMResult


@dataclass
class EvalReport:
    accepted: bool
    code: FailCode
    detail: str
    txid: str
    digest_hex: str
    inputs: list[InputEval] = field(default_factory=list)
    total_in: int = 0
    total_out: int = 0

    @property
    def kind(self):
        from stackvm.errors import kind_of
        return kind_of(self.code)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "kind": self.kind.value,
            "code": self.code.value,
            "detail": self.detail,
            "txid": self.txid,
            "digest": self.digest_hex,
            "total_in": self.total_in,
            "total_out": self.total_out,
            "inputs": [
                {
                    "index": e.index,
                    "prevout": f"{e.prev_txid}:{e.prev_vout}",
                    "prev_script": e.prev_script,
                    "value": e.value,
                    "ok": e.result.ok,
                    "code": e.result.code.value,
                    "detail": e.result.detail,
                    "budget_left": e.result.budget_left,
                    "trace": [
                        {"pc": t.pc, "op": t.op, "offset": t.offset,
                         "budget_left": t.budget_left, "active": t.active,
                         "stack": t.stack, "alt": t.alt, "note": t.note}
                        for t in e.result.trace
                    ],
                    "checks": e.result.checks,
                }
                for e in self.inputs
            ],
        }


class ChainKernel:
    def __init__(self, store, settings: Settings):
        self.store = store
        self.settings = settings

    # ---------------- 纯验证（不写状态） ----------------

    def evaluate(self, tx: Transaction) -> EvalReport:
        tid = txid_of(tx)
        total_out = sum(o.value for o in tx.outputs)

        prev_scripts: list[bytes] = []
        values: list[int] = []
        evals: list[InputEval] = []

        # 1) 状态检查：重复交易、UTXO 存在性
        if self.store.has_transaction(tid):
            return self._fail(FailCode.TX_ALREADY_ACCEPTED, tid,
                              f"交易 {tid} 已在链上，禁止重复入账", b"")

        total_in = 0
        seen_outpoints: set[tuple[str, int]] = set()
        for idx, inp in enumerate(tx.inputs):
            outpoint = (inp.txid, inp.vout)
            if outpoint in seen_outpoints:
                return self._fail(FailCode.TX_MALFORMED, tid,
                                  f"输入 {idx} 重复引用同一 UTXO {inp.txid}:{inp.vout}",
                                  b"")
            seen_outpoints.add(outpoint)
            row = self.store.get_utxo(inp.txid, inp.vout)
            if row is None:
                return self._fail(FailCode.UTXO_MISSING, tid,
                                  f"输入 {idx} 引用的 UTXO {inp.txid}:{inp.vout} "
                                  f"不存在或已被花费", b"")
            value, script_hex = row
            total_in += value
            values.append(value)
            prev_scripts.append(bytes.fromhex(script_hex))

        # 2) 金额平衡
        if total_in != total_out:
            digest = signature_digest(tx, prev_scripts, self.settings.sighash.domain_tag)
            return self._fail(
                FailCode.VALUE_IMBALANCE, tid,
                f"输入合计 {total_in} != 输出合计 {total_out}（无费模型要求严格相等）",
                digest,
            )

        # 3) 逐输入执行脚本（同一交易摘要绑定全部输入与域标签）
        digest = signature_digest(tx, prev_scripts, self.settings.sighash.domain_tag)
        for idx, inp in enumerate(tx.inputs):
            unlock = bytes.fromhex(inp.unlock)
            result = run_scripts(tx, digest, unlock, prev_scripts[idx],
                                 limits=self.settings.limits)
            evals.append(InputEval(
                index=idx, prev_txid=inp.txid, prev_vout=inp.vout,
                prev_script=prev_scripts[idx].hex(), value=values[idx], result=result))
            if not result.ok:
                return EvalReport(
                    accepted=False, code=result.code, detail=result.detail,
                    txid=tid, digest_hex=digest.hex(), inputs=evals,
                    total_in=total_in, total_out=total_out)

        return EvalReport(
            accepted=True, code=FailCode.OK, detail="全部输入验证通过",
            txid=tid, digest_hex=digest.hex(), inputs=evals,
            total_in=total_in, total_out=total_out)

    def submit(self, tx: Transaction) -> EvalReport:
        """验证 + 原子提交；失败只返回分类，绝不执行转账。"""
        report = self.evaluate(tx)
        if not report.accepted:
            return report
        self.store.apply_transaction(
            tx,
            [{"txid": e.prev_txid, "vout": e.prev_vout} for e in report.inputs],
            state_root_note=report.digest_hex,
        )
        return report

    @staticmethod
    def _fail(code: FailCode, tid: str, detail: str, digest: bytes) -> EvalReport:
        return EvalReport(accepted=False, code=code, detail=detail, txid=tid,
                          digest_hex=digest.hex())
