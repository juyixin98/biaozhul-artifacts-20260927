"""YAML 夹具场景执行器。

夹具同时驱动两套实现：

1. **被测服务**（``Kernel`` + SQLite，真实存储/审计/重分类）；
2. :mod:`local_txpool.offline.oracle` 中的 **独立参考预言机**（手写字典模型）。

每一步之后对两边做差分断言，并在标记 ``expect`` 的检查点断言**手写预期值**
（具体状态、候选顺序、失败类别、余额/nonce）。因此测试答案不是由被测核心
"自己生成、自己断言"。

夹具步骤::

    fund:           {account, balance, credit}
    submit:         {tx, expect_accepted, expect_error}
    advance_ms:     毫秒数
    expire:         ~
    propose:        {external:[...], expect_order:[tx...], expect_applied:[...]}
    confirm:        ~
    rollback:       {target, expect_reentered_n: n}
    assert_snapshot: 任意检查点字段

交易在 fixtures 的 transactions 段以助记名定义，运行时由本地合成私钥真实签名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core import crypto
from ..core.clock import FakeClock
from ..core.config import Config
from ..core.kernel import Kernel
from ..core.models import ErrorCode, Transaction
from ..storage.repository import Repository, connect, init_schema
from .oracle import Oracle, OracleConfig


class ScenarioAssertionError(AssertionError):
    """夹具检查点失败：消息含期望值/实际值与步骤序号。"""


@dataclass
class StepReport:
    index: int
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScenarioRun:
    name: str
    reports: list[StepReport] = field(default_factory=list)
    # 助记名 -> tx_hash
    tx_hashes: dict[str, str] = field(default_factory=dict)


class ScenarioRunner:
    def __init__(
        self,
        scenario: dict[str, Any],
        *,
        config_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.scenario = scenario
        self.cfg = self._build_config(scenario, config_overrides or {})
        self.clock = FakeClock(
            int(scenario.get("start_ms", 1_700_000_000_000))
        )
        conn = connect(":memory:")
        init_schema(conn)
        self.repo = Repository(conn)
        self.kernel = Kernel(self.repo, self.cfg, self.clock)
        self.oracle = Oracle(self._oracle_config(self.cfg))
        self.run = ScenarioRun(name=scenario.get("name", "scenario"))
        # 助记私钥：确定性 keccak 派生（合成环境专用）
        self.keys: dict[str, bytes] = {}
        self.txs: dict[str, Transaction] = {}

    # ---------------- 构建 ---------------- #
    @staticmethod
    def _build_config(
        scenario: dict[str, Any], overrides: dict[str, Any]
    ) -> Config:
        cfg = Config()
        cfg_dict = scenario.get("config", {}) or {}
        cfg_dict = {**cfg_dict, **overrides}
        if "block_gas_limit" in cfg_dict:
            cfg.gas.block_gas_limit = int(cfg_dict["block_gas_limit"])
        if "min_gas_price_wei" in cfg_dict:
            cfg.gas.min_gas_price_wei = int(cfg_dict["min_gas_price_wei"])
        if "intrinsic_gas" in cfg_dict:
            cfg.gas.tx_intrinsic_gas = int(cfg_dict["intrinsic_gas"])
        if "data_gas_per_byte" in cfg_dict:
            cfg.gas.data_gas_per_byte = int(cfg_dict["data_gas_per_byte"])
        if "max_transactions" in cfg_dict:
            cfg.pool.max_transactions = int(cfg_dict["max_transactions"])
        if "max_queued_per_sender" in cfg_dict:
            cfg.pool.max_queued_per_sender = int(
                cfg_dict["max_queued_per_sender"]
            )
        if "max_transactions_per_sender" in cfg_dict:
            cfg.pool.max_transactions_per_sender = int(
                cfg_dict["max_transactions_per_sender"]
            )
        if "replacement_price_bump_pct" in cfg_dict:
            cfg.pool.replacement_price_bump_pct = int(
                cfg_dict["replacement_price_bump_pct"]
            )
        if "pending_ttl_seconds" in cfg_dict:
            cfg.pool.pending_ttl_seconds = int(
                cfg_dict["pending_ttl_seconds"]
            )
        if "confirmation_depth" in cfg_dict:
            cfg.finality.confirmation_depth = int(
                cfg_dict["confirmation_depth"]
            )
        if "chain_id" in cfg_dict:
            cfg.chain.chain_id = int(cfg_dict["chain_id"])
        return cfg

    @staticmethod
    def _oracle_config(cfg: Config) -> OracleConfig:
        return OracleConfig(
            block_gas_limit=cfg.gas.block_gas_limit,
            confirmation_depth=cfg.finality.confirmation_depth,
            min_gas_price=cfg.gas.min_gas_price_wei,
            intrinsic_gas=cfg.gas.tx_intrinsic_gas,
            data_gas_per_byte=cfg.gas.data_gas_per_byte,
            max_queued_per_sender=cfg.pool.max_queued_per_sender,
            replacement_bump_pct=cfg.pool.replacement_price_bump_pct,
            max_transactions=cfg.pool.max_transactions,
            pending_ttl_ms=cfg.pool.pending_ttl_seconds * 1000,
            max_transactions_per_sender=cfg.pool.max_transactions_per_sender,
        )

    def _key(self, name: str) -> bytes:
        if name not in self.keys:
            # 合成确定性私钥：keccak("local-txpool/synthetic:"+name) 取 32B
            from eth_hash.auto import keccak

            self.keys[name] = keccak(
                f"local-txpool/synthetic:{name}".encode()
            )
        return self.keys[name]

    def address(self, account: str) -> str:
        return crypto.address_for_private_key(self._key(account))

    def _build_tx(self, spec: dict[str, Any]) -> Transaction:
        signer = spec["signer"]
        return crypto.sign_transaction(
            private_key=self._key(signer),
            nonce=int(spec["nonce"]),
            gas_price=int(spec["gas_price"]),
            gas_limit=int(spec.get("gas_limit", 21_000)),
            to=self.address(spec["to"]) if "to" in spec else "0x" + "11" * 20,
            value=int(spec.get("value", 0)),
            data=bytes.fromhex(str(spec.get("data_hex", ""))),
            chain_id=self.cfg.chain.chain_id,
        )

    # ---------------- 解析助记 ---------------- #
    def _resolve_tx_list(self, refs: list[str]) -> list[str]:
        return [self.run.tx_hashes[r] for r in refs]

    # ---------------- 主执行 ---------------- #
    def execute(self) -> ScenarioRun:
        for idx, step in enumerate(self.scenario.get("steps", []), start=1):
            kind = step["step"]
            handler = getattr(self, f"_step_{kind}", None)
            if handler is None:
                raise ScenarioAssertionError(
                    f"步骤 {idx}: 未知步骤类型 {kind}"
                )
            detail = handler(step)
            self.run.reports.append(
                StepReport(index=idx, kind=kind, detail=detail or {})
            )
            self._check_expect(idx, step)
            self._differential_check(idx)
        return self.run

    # ----- 各步骤 ----- #
    def _step_fund(self, step: dict[str, Any]) -> dict[str, Any]:
        account = step["account"]
        address = self.address(account)
        balance = int(step["balance"])
        credit = bool(step.get("credit", True))
        if credit:
            self.kernel.create_or_fund_account(
                address, balance, request_id=f"fund-{account}"
            )
            self.oracle.fund(address, balance)
        else:
            with self.repo.transaction() as conn:
                self.repo.upsert_account(conn, address, balance)
            self.oracle.fund(address, balance, reset=True)
        return {"address": address, "balance": balance}

    def _step_submit(self, step: dict[str, Any]) -> dict[str, Any]:
        name = step["tx"]
        spec = self.scenario["transactions"][name]
        tx = self._build_tx(spec)
        self.txs[name] = tx
        self.run.tx_hashes[name] = tx.tx_hash

        result = self.kernel.submit_transaction(
            tx, request_id=f"submit-{name}"
        )
        accepted, code = self.oracle.submit(tx)
        return {
            "tx": name,
            "tx_hash": tx.tx_hash,
            "accepted": result.accepted,
            "error": result.error_code.value if result.error_code else None,
            "oracle_accepted": accepted,
            "oracle_error": code,
            "status": result.status.value if result.status else None,
        }

    def _step_advance_ms(self, step: dict[str, Any]) -> dict[str, Any]:
        ms = int(step["ms"])
        self.clock.advance(ms)
        self.oracle.advance_ms(ms)
        return {"advanced_ms": ms, "now_ms": self.clock.now_ms()}

    def _step_expire(self, step: dict[str, Any]) -> dict[str, Any]:
        expired = self.kernel.expire_pending(request_id="expire")
        oracle_expired = self.oracle.expire()
        return {"expired": expired, "oracle_expired": oracle_expired}

    def _step_propose(self, step: dict[str, Any]) -> dict[str, Any]:
        block, plan, rejected = self.kernel.propose_block(
            request_id=f"propose-{len(self.oracle.state.blocks) + 1}"
        )
        oracle_block = self.oracle.propose()
        return {
            "number": block.number,
            "ordered": [s.tx.tx_hash for s in plan.ordered],
            "applied": list(block.executed_tx_hashes),
            "oracle_applied": oracle_block.applied,
            "rejected_external": len(rejected),
        }

    def _step_confirm(self, step: dict[str, Any]) -> dict[str, Any]:
        confirmed = self.kernel.confirm_depth(request_id="confirm")
        self.oracle.finalize()
        return {"confirmed": confirmed}

    def _step_rollback(self, step: dict[str, Any]) -> dict[str, Any]:
        target = int(step["target"])
        try:
            result = self.kernel.rollback_to(target, request_id="rollback")
        except Exception as exc:  # 最终化拒绝等
            code = getattr(exc, "code", None)
            self.run.reports[-1:] if self.run.reports else None
            return {
                "rolled_back": [],
                "error": code.value if code else "conflict",
            }
        try:
            oracle_reentered = self.oracle.rollback_to(target)
        except ValueError as exc:
            return {"rolled_back": result["reentered"],
                    "error": str(exc)}
        return {
            "rolled_back": result["reentered"],
            "oracle_reentered": oracle_reentered,
        }

    def _step_assert_integrity(self, step: dict[str, Any]) -> dict[str, Any]:
        self.kernel.assert_integrity()
        return {"integrity": "ok"}

    # ---------------- 检查点：手写预期 ---------------- #
    def _check_expect(self, idx: int, step: dict[str, Any]) -> None:
        detail = self.run.reports[-1].detail

        if "expect_accepted" in step:
            actual = detail.get("accepted")
            if bool(step["expect_accepted"]) != bool(actual):
                raise ScenarioAssertionError(
                    f"步骤 {idx} ({step['step']}): expect_accepted="
                    f"{step['expect_accepted']} 实际 accepted={actual} "
                    f"error={detail.get('error')}"
                )
        if "expect_error" in step:
            actual = detail.get("error")
            wanted = step["expect_error"]
            if actual != wanted:
                raise ScenarioAssertionError(
                    f"步骤 {idx} ({step['step']}): expect_error={wanted} "
                    f"实际 error={actual}"
                )
        if "expect_order" in step:
            want = self._resolve_tx_list(step["expect_order"])
            actual = detail.get("ordered", [])
            if want != actual:
                raise ScenarioAssertionError(
                    f"步骤 {idx} (propose): 候选顺序不一致\n"
                    f"  期望 {[self._name_of(h) for h in want]}\n"
                    f"  实际 {[self._name_of(h) for h in actual]}"
                )
        if "expect_applied" in step:
            want = self._resolve_tx_list(step["expect_applied"])
            actual = detail.get("applied", [])
            if want != actual:
                raise ScenarioAssertionError(
                    f"步骤 {idx} (propose): 执行集合不一致\n"
                    f"  期望 {[self._name_of(h) for h in want]}\n"
                    f"  实际 {[self._name_of(h) for h in actual]}"
                )
        if "expect_reentered_n" in step:
            actual = len(detail.get("rolled_back", []))
            if actual != int(step["expect_reentered_n"]):
                raise ScenarioAssertionError(
                    f"步骤 {idx} (rollback): 重入数量期望 "
                    f"{step['expect_reentered_n']} 实际 {actual}"
                )
        self._check_snapshot(idx, step.get("expect", {}))

    def _name_of(self, tx_hash: str) -> str:
        for name, h in self.run.tx_hashes.items():
            if h == tx_hash:
                return name
        return tx_hash[:10]

    def _check_snapshot(self, idx: int, expect: dict[str, Any]) -> None:
        if not expect:
            return
        if "pool" in expect:
            pool = self.kernel.list_pool()
            for field_name in ("pending", "queued"):
                if field_name in expect["pool"]:
                    want = set(expect["pool"][field_name])
                    actual_names = {
                        self._name_of(item["tx_hash"]) for item in pool[field_name]
                    }
                    if want != actual_names:
                        raise ScenarioAssertionError(
                            f"步骤 {idx}: pool.{field_name} 期望 {sorted(want)} "
                            f"实际 {sorted(actual_names)}"
                        )
        if "candidate" in expect:
            cand = self.kernel.preview_candidate()
            actual = [self._name_of(c["tx_hash"]) for c in cand["ordered"]]
            if list(expect["candidate"]) != actual:
                raise ScenarioAssertionError(
                    f"步骤 {idx}: candidate 期望 {expect['candidate']} 实际 {actual}"
                )
        if "accounts" in expect:
            for name, fields in expect["accounts"].items():
                acct = self.kernel.get_account(self.address(name))
                if acct is None:
                    raise ScenarioAssertionError(
                        f"步骤 {idx}: 账户 {name} 不存在"
                    )
                for fname, want in fields.items():
                    actual = getattr(acct, fname)
                    if int(want) != int(actual):
                        raise ScenarioAssertionError(
                            f"步骤 {idx}: 账户 {name}.{fname} 期望 {want} "
                            f"实际 {actual}"
                        )
        if "tx_status" in expect:
            for name, want_status in expect["tx_status"].items():
                stored = self.kernel.get_tx(self.run.tx_hashes[name])
                if stored is None:
                    raise ScenarioAssertionError(
                        f"步骤 {idx}: 交易 {name} 不存在"
                    )
                if stored.status.value != want_status:
                    raise ScenarioAssertionError(
                        f"步骤 {idx}: 交易 {name} 状态期望 {want_status} "
                        f"实际 {stored.status.value}"
                    )
        if "head_height" in expect:
            if self.kernel.chain_head()["height"] != int(expect["head_height"]):
                raise ScenarioAssertionError(
                    f"步骤 {idx}: head_height 期望 {expect['head_height']} "
                    f"实际 {self.kernel.chain_head()['height']}"
                )
        if "integrity" in expect and expect["integrity"]:
            self.kernel.assert_integrity()

    # ---------------- 差分：服务 vs 独立预言机 ---------------- #
    def _kernel_snapshot(self) -> dict[str, Any]:
        pool = self.kernel.list_pool()
        accounts = {
            a.address: {"balance": a.balance, "nonce": a.nonce}
            for a in self.kernel.list_accounts()
        }
        blocks: list[dict[str, Any]] = []
        with self.repo.transaction() as conn:
            txs = self._tx_map_from_db(conn)
            head = self.repo.head_block(conn)
            height = head.number if head else 0
            for n in range(1, height + 1):
                b = self.repo.get_block_by_number(conn, n)
                if b:
                    blocks.append(
                        {"number": n, "applied": list(b.executed_tx_hashes)}
                    )
            proposed_rows = conn.execute(
                "SELECT tx_hash, proposed_block FROM transactions "
                "WHERE status='proposed'"
            ).fetchall()
            block_of = {r["tx_hash"]: r["proposed_block"] for r in proposed_rows}
        for h, entry in txs.items():
            entry["block"] = block_of.get(h)
        return {
            "accounts": dict(sorted(accounts.items())),
            "txs": dict(sorted(txs.items())),
            "pending": sorted(t["tx_hash"] for t in pool["pending"]),
            "queued": sorted(t["tx_hash"] for t in pool["queued"]),
            "blocks": blocks,
            "candidate": [c["tx_hash"] for c in
                          self.kernel.preview_candidate()["ordered"]],
        }

    def _tx_map_from_db(self, conn) -> dict[str, dict[str, Any]]:
        from ..storage.repository import _row_to_tx

        out: dict[str, dict[str, Any]] = {}
        rows = conn.execute(
            "SELECT * FROM transactions WHERE status IN "
            "('pending','queued','proposed','confirmed','dropped','rolled_back')"
        ).fetchall()
        for r in rows:
            s = _row_to_tx(r)
            out[s.tx.tx_hash] = {
                "status": s.status.value,
                "nonce": s.tx.nonce,
                "sender": s.tx.sender,
                "block": None,
            }
        return out

    def _differential_check(self, idx: int) -> None:
        actual = self._kernel_snapshot()
        expected = self.oracle.snapshot()

        # 账户余额/nonce
        if actual["accounts"] != expected["accounts"]:
            raise ScenarioAssertionError(
                f"步骤 {idx}: 账户状态差分不一致\n"
                f"  服务 {actual['accounts']}\n"
                f"  预言机 {expected['accounts']}"
            )
        # 池内分类
        if actual["pending"] != expected["pending"]:
            raise ScenarioAssertionError(
                f"步骤 {idx}: pending 集合差分不一致\n"
                f"  服务 {[self._name_of(h) for h in actual['pending']]}\n"
                f"  预言机 {[self._name_of(h) for h in expected['pending']]}"
            )
        if actual["queued"] != expected["queued"]:
            raise ScenarioAssertionError(
                f"步骤 {idx}: queued 集合差分不一致\n"
                f"  服务 {[self._name_of(h) for h in actual['queued']]}\n"
                f"  预言机 {[self._name_of(h) for h in expected['queued']]}"
            )
        # 交易状态（仅比较双方都认识的哈希）
        common = set(actual["txs"]) & set(expected["txs"])
        for h in sorted(common):
            a, e = actual["txs"][h], expected["txs"][h]
            if a["status"] != e["status"]:
                raise ScenarioAssertionError(
                    f"步骤 {idx}: 交易 {self._name_of(h)} 状态差分不一致 "
                    f"服务={a['status']} 预言机={e['status']}"
                )
        # 候选顺序
        if actual["candidate"] != expected["candidate"]:
            raise ScenarioAssertionError(
                f"步骤 {idx}: 候选顺序差分不一致\n"
                f"  服务 {[self._name_of(h) for h in actual['candidate']]}\n"
                f"  预言机 {[self._name_of(h) for h in expected['candidate']]}"
            )
        # 区块执行序列
        if [b["applied"] for b in actual["blocks"]] != [
            b["applied"] for b in expected["blocks"]
        ]:
            raise ScenarioAssertionError(
                f"步骤 {idx}: 区块执行序列差分不一致\n"
                f"  服务 {actual['blocks']}\n"
                f"  预言机 {expected['blocks']}"
            )


def load_scenario(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "steps" not in data:
        raise ValueError(f"夹具 {path} 必须是含 steps 的映射")
    return data


def run_scenario_file(path: str | Path) -> ScenarioRun:
    scenario = load_scenario(path)
    return ScenarioRunner(scenario).execute()
