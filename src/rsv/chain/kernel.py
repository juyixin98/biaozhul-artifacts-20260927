"""链状态内核（模块二）。

把编码/验签（模块一）、栈机与索引存储（模块三）粘起来，负责一笔交易的
完整验证与原子上链：

1. 结构校验（input.*）
2. 状态前置检查：outpoint 存在、未被花费、域一致（state.*）
3. 金额守恒（state.imbalance）
4. 逐输入构造绑定交易摘要+域标签的 message，执行解锁+锁定脚本
   （resource.* / compute.*）
5. 全部通过才在*单个数据库事务*内删除输入 UTXO、登记 spent、写入输出 UTXO；
   任何失败都在变更前抛出，因此“只返回失败分类，绝不执行转账”。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..encoding import crypto
from ..encoding.transaction import Transaction, transaction_from_dict
from ..errors import (
    ALREADY_SPENT,
    DOMAIN_CONFLICT,
    IMBALANCE,
    UNKNOWN_OUTPOINT,
    VerificationFailure,
)
from ..storage import RunRecord, SQLiteStore, UTXORecord
from ..vm import RunContext, StackMachine


@dataclass
class VerifyReport:
    accepted: bool
    run_id: str
    txid: str
    failure: dict | None = None
    message32: str | None = None
    per_input: list[dict] = field(default_factory=list)
    steps_used: int = 0
    state_root_after: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "run_id": self.run_id,
            "txid": self.txid,
            "failure": self.failure,
            "message32": self.message32,
            "per_input": self.per_input,
            "steps_used": self.steps_used,
            "state_root_after": self.state_root_after,
            "reason": self.reason,
        }


class ChainKernel:
    def __init__(self, settings: Settings, store: SQLiteStore | None = None):
        self.settings = settings
        self.store = store or SQLiteStore(settings.sqlite_path)
        Path(settings.runs_dir).mkdir(parents=True, exist_ok=True)
        self._counter = 0

    def new_run_id(self, kind: str) -> str:
        self._counter += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{kind}-{ts}-{os.getpid()}-{self._counter:04d}-{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    def verify_transaction_dict(
        self, tx_dict: dict, *, persist: bool, kind: str = "verify"
    ) -> VerifyReport:
        run_id = self.new_run_id(kind)
        try:
            tx = transaction_from_dict(tx_dict, self.settings.chain)
        except VerificationFailure as vf:
            return self._reject(run_id, None, vf, persist=False, kind=kind, tx_dict=tx_dict)

        txid = tx.txid().hex()
        try:
            report = self._verify(tx, run_id)
        except VerificationFailure as vf:
            return self._reject(run_id, txid, vf, persist=False, kind=kind)

        if persist:
            self._apply(tx, run_id)
            report.state_root_after = self.store.state_root()
            report.reason = "all inputs verified; UTXOs spent and outputs added atomically"
            self._record(report, kind=kind, reason=report.reason)
        else:
            report.state_root_after = self.store.state_root()
            report.reason = (
                "dry-run verified: all inputs passed; no state change performed"
            )
            self._record(report, kind=kind, reason=report.reason)
        return report

    # ------------------------------------------------------------------
    def _verify(self, tx: Transaction, run_id: str) -> VerifyReport:
        canonical = tx.canonical()
        message32 = crypto.build_message(self.settings.chain.network, tx.domain, canonical)

        # ---- 状态前置检查 + 金额守恒 ----
        in_sum = 0
        prevouts: list[UTXORecord] = []
        seen_outpoints: set[tuple[str, int]] = set()
        for i, inp in enumerate(tx.inputs):
            key = (inp.outpoint.txid, inp.outpoint.index)
            if key in seen_outpoints:
                raise VerificationFailure(
                    ALREADY_SPENT, f"input #{i} duplicates outpoint {key} in same tx",
                )
            seen_outpoints.add(key)

            utxo = self.store.get_utxo(inp.outpoint.txid, inp.outpoint.index)
            if utxo is None:
                if self.store.is_spent(inp.outpoint.txid, inp.outpoint.index):
                    raise VerificationFailure(
                        ALREADY_SPENT,
                        f"input #{i} {inp.outpoint.txid[:12]}:{inp.outpoint.index}",
                    )
                raise VerificationFailure(
                    UNKNOWN_OUTPOINT,
                    f"input #{i} {inp.outpoint.txid[:12]}:{inp.outpoint.index}",
                )
            if utxo.domain != tx.domain:
                raise VerificationFailure(
                    DOMAIN_CONFLICT,
                    f"input #{i} utxo domain={utxo.domain!r} != tx domain={tx.domain!r}",
                )
            if utxo.value != inp.value:
                from ..errors import MALFORMED_TX

                raise VerificationFailure(
                    MALFORMED_TX,
                    f"input #{i} declared value {inp.value} != utxo {utxo.value}",
                )
            in_sum += utxo.value
            prevouts.append(utxo)

        out_sum = sum(o.value for o in tx.outputs)
        if in_sum != out_sum:
            raise VerificationFailure(
                IMBALANCE, f"sum(inputs)={in_sum} != sum(outputs)={out_sum}"
            )

        # ---- 逐输入执行脚本 ----
        machine = StackMachine(
            self.settings.limits,
            require_clean_stack=self.settings.chain.require_clean_stack,
        )
        per_input: list[dict] = []
        total_steps = 0
        for i, (inp, utxo) in enumerate(zip(tx.inputs, prevouts)):
            trace: list[str] = []
            ctx = RunContext(
                message32=message32,
                network=self.settings.chain.network,
                domain=tx.domain,
            )
            res = machine.execute(
                inp.witness_script,
                bytes.fromhex(utxo.pubkey_script),
                ctx,
                trace=trace,
            )
            total_steps += res.steps_used
            per_input.append(
                {
                    "index": i,
                    "outpoint": f"{inp.outpoint.txid}:{inp.outpoint.index}",
                    "steps_used": res.steps_used,
                    "final_stack": [x.hex() for x in res.final_stack],
                    "trace": trace,
                }
            )

        return VerifyReport(
            accepted=True,
            run_id=run_id,
            txid=tx.txid().hex(),
            failure=None,
            message32=message32.hex(),
            per_input=per_input,
            steps_used=total_steps,
        )

    # ------------------------------------------------------------------
    def _apply(self, tx: Transaction, run_id: str) -> None:
        """原子状态转移：失败回滚，不留半笔账。"""
        txid = tx.txid().hex()
        conn = self.store.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for inp in tx.inputs:
                # 事务内复查，防止并发/重放造成双花
                row = conn.execute(
                    "SELECT 1 FROM utxo WHERE txid=? AND idx=?",
                    (inp.outpoint.txid, inp.outpoint.index),
                ).fetchone()
                if row is None:
                    raise VerificationFailure(
                        ALREADY_SPENT,
                        f"apply-time missing {inp.outpoint.txid[:12]}:{inp.outpoint.index}",
                    )
                conn.execute(
                    "INSERT INTO spent(txid, idx, spent_txid, spent_run) VALUES (?,?,?,?)",
                    (inp.outpoint.txid, inp.outpoint.index, txid, run_id),
                )
                conn.execute(
                    "DELETE FROM utxo WHERE txid=? AND idx=?",
                    (inp.outpoint.txid, inp.outpoint.index),
                )
            for j, out in enumerate(tx.outputs):
                conn.execute(
                    "INSERT INTO utxo(txid, idx, value, pubkey_script, domain, created_run)"
                    " VALUES (?,?,?,?,?,?)",
                    (txid, j, out.value, out.pubkey_script.hex(), tx.domain, run_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    def bootstrap_from_dict(self, genesis: dict) -> dict:
        existing = self.store.get_meta("genesis_id")
        gid = genesis.get("genesis_id") or _hash_genesis(genesis)
        if existing is not None and existing != gid:
            from ..errors import BOOTSTRAP_CONFLICT

            raise VerificationFailure(
                BOOTSTRAP_CONFLICT, f"existing genesis {existing[:12]} != {gid[:12]}"
            )
        if existing == gid:
            return {"bootstrapped": False, "genesis_id": gid, "reason": "already initialized"}

        run_id = self.new_run_id("bootstrap")
        conn = self.store.connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            for coin in genesis.get("utxos", []):
                rec = UTXORecord(
                    txid=coin["txid"],
                    idx=int(coin["index"]),
                    value=int(coin["value"]),
                    pubkey_script=coin["pubkey_script"],
                    domain=coin.get("domain", self.settings.chain.default_domain),
                    created_run=run_id,
                )
                conn.execute(
                    "INSERT OR FAIL INTO utxo(txid, idx, value, pubkey_script, domain, created_run)"
                    " VALUES (?,?,?,?,?,?)",
                    (rec.txid, rec.idx, rec.value, rec.pubkey_script, rec.domain, run_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        self.store.set_meta("genesis_id", gid)
        return {"bootstrapped": True, "genesis_id": gid, "utxos": len(genesis.get("utxos", []))}

    # ------------------------------------------------------------------
    def _reject(
        self,
        run_id: str,
        txid: str | None,
        vf: VerificationFailure,
        *,
        persist: bool,
        kind: str,
        tx_dict: dict | None = None,
    ) -> VerifyReport:
        del persist  # 拒绝路径永远不改状态
        report = VerifyReport(
            accepted=False,
            run_id=run_id,
            txid=txid or "",
            failure=vf.to_dict(),
            reason=_explain(vf),
        )
        self._record(report, kind=kind, reason=report.reason, tx_dict=tx_dict)
        return report

    def _record(
        self,
        report: VerifyReport,
        *,
        kind: str,
        reason: str,
        tx_dict: dict | None = None,
    ) -> None:
        traces: list[str] = []
        for pi in report.per_input:
            traces.extend(f"in#{pi['index']}: {line}" for line in pi["trace"])
        rec = RunRecord(
            run_id=report.run_id,
            ts=datetime.now(timezone.utc).isoformat(),
            kind=kind,
            accepted=report.accepted,
            txid=report.txid or None,
            category=report.failure["category"] if report.failure else None,
            code=report.failure["code"] if report.failure else None,
            detail=report.failure["detail"] if report.failure else None,
            message32=report.message32,
            trace=traces[: self.settings.limits.max_trace_items],
            reason=reason,
        )
        self.store.insert_run(rec)
        # 同时落 JSONL 运行日志（可脱离 DB 重放问题）
        log_path = Path(self.settings.runs_dir) / "runs.jsonl"
        entry = {
            "run_id": rec.run_id,
            "ts": rec.ts,
            "kind": rec.kind,
            "accepted": rec.accepted,
            "txid": rec.txid,
            "category": rec.category,
            "code": rec.code,
            "detail": rec.detail,
            "message32": rec.message32,
            "reason": rec.reason,
            "steps_used": report.steps_used,
            "state_root_after": report.state_root_after,
            "trace": rec.trace,
            "tx_submitted": tx_dict,
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _explain(vf: VerificationFailure) -> str:
    c = vf.code.category
    base = {
        "input": "输入错误：结构/编码不合法，交易未进入执行",
        "state": "状态冲突：链状态前置检查未通过，未执行脚本、未转账",
        "resource": "资源耗尽：超过元素/步数/深度预算，执行中止，未转账",
        "compute": "计算失败：脚本执行判定不通过，未转账",
    }[c]
    return f"{base}；判定码 {vf.code.code}：{vf.detail or vf.code.message}"


def _hash_genesis(genesis: dict) -> str:
    import hashlib

    canonical = json.dumps(genesis, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()
