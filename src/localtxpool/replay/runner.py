"""离线回放引擎：事件 JSONL + 确定性虚拟时钟，与在线服务共用同一内核。

事件类型（每行一个 JSON 对象，``at`` 为 Unix 秒，时钟只前进）::

    {"at":100,"type":"clock"}                         仅推进时钟
    {"at":100,"type":"fund","account":"alice","key":"<0x32B>","balance":"10**18"}
    {"at":101,"type":"tx","account":"alice","nonce":0,"to":"bob",
     "gas_price":10,"gas_limit":21000,"value":1000,"data":"0x"}
    {"at":102,"type":"raw","raw":"0x..."}             直接喂入已编码交易（验签路径）
    {"at":103,"type":"propose","gas_limit":10000000,"coinbase":"miner"}
    {"at":104,"type":"confirm"}
    {"at":105,"type":"discard"}
    {"at":106,"type":"rollback","n":1}
    {"at":107,"type":"reap"}

余额/金额字段写十进制字符串（或数字），内部用 ``int`` 解析。
每个事件产出一条 trace：成功记录关键结果，失败记录稳定错误码——
黄金文件对两者都断言（包括失败类别）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from eth_keys import keys as ek_keys

from ..clock import VirtualClock
from ..config import Config
from ..encoding import sign_transaction, to_checksum_address
from ..errors import PoolError
from ..service import Service


class ReplayAbort(Exception):
    """事件文件本身有问题（区别于业务 PoolError）。"""


@dataclass
class Trace:
    index: int
    at: int
    type: str
    ok: bool
    result: dict

    def to_dict(self) -> dict:
        return {"index": self.index, "at": self.at, "type": self.type,
                "ok": self.ok, "result": self.result}


class Replayer:
    def __init__(self, config: Config, start_time: int = 0):
        self.clock = VirtualClock(start_time)
        self.service = Service.in_memory(config, clock=self.clock)
        self.accounts: dict[str, dict] = {}  # label -> {key, address}
        self.traces: list[Trace] = []
        # 每个 snapshot 事件发生时记录的内核快照（黄金文件按事件索引核对）
        self.snapshots: dict[int, dict] = {}

    # ------------------------------------------------------------------ #
    def _resolve_account(self, label_or_addr: str) -> tuple[str, bytes]:
        """返回 (checksum 地址, 20字节地址)。label 必须先由 fund 事件声明。"""
        if label_or_addr in self.accounts:
            return self.accounts[label_or_addr]["address"], \
                bytes.fromhex(self.accounts[label_or_addr]["address"][2:])
        if label_or_addr.startswith("0x") and len(label_or_addr) == 42:
            return to_checksum_address(bytes.fromhex(label_or_addr[2:])), \
                bytes.fromhex(label_or_addr[2:])
        raise ReplayAbort(f"unknown account label {label_or_addr!r}; "
                          "declare it with a fund event first")

    def run_event(self, index: int, ev: dict) -> Trace:
        etype = ev.get("type")
        at = int(ev["at"])
        self.clock.advance_to(at)
        s = self.service
        req = f"replay#{index}"
        result: dict

        try:
            if etype == "clock":
                result = {"now": self.clock.now()}

            elif etype == "fund":
                label = ev["account"]
                key_hex = ev.get("key")
                if key_hex:
                    pk = bytes.fromhex(key_hex[2:] if key_hex.startswith("0x") else key_hex)
                    addr = ek_keys.PrivateKey(pk).public_key.to_checksum_address()
                    self.accounts[label] = {"key": key_hex, "address": addr}
                else:
                    if label not in self.accounts:
                        raise ReplayAbort(f"fund {label!r} without key")
                    addr = self.accounts[label]["address"]
                amount = int(ev["balance"])
                with s.repo.transaction():
                    s.repo.ensure_account(addr.lower(), at)
                    new_balance = s.repo.adjust_balance(addr.lower(), amount, at)
                    if "nonce" in ev:
                        row = s.repo.get_account(addr.lower())
                        s.repo.set_account(addr.lower(), int(row["balance"]), int(ev["nonce"]), at)
                    s.pool.classify_sender(addr.lower(), request_id=req, reason="BALANCE_CHANGED")
                result = {"account": label, "address": addr,
                          "balance": str(new_balance), "nonce": ev.get("nonce", 0)}

            elif etype == "tx":
                if ev["account"] not in self.accounts:
                    raise ReplayAbort(f"tx from undeclared label {ev['account']!r}")
                key_hex = self.accounts[ev["account"]]["key"]
                pk = bytes.fromhex(key_hex[2:] if key_hex.startswith("0x") else key_hex)
                sender_addr = self.accounts[ev["account"]]["address"]
                to_raw = b""
                if ev.get("to"):
                    _, to_raw = self._resolve_account(ev["to"])
                data = bytes.fromhex(ev.get("data", "0x")[2:])
                tx = sign_transaction(
                    pk,
                    nonce=int(ev["nonce"]),
                    gas_price=int(ev["gas_price"]),
                    gas_limit=int(ev.get("gas_limit", 21000)),
                    to=to_raw,
                    value=int(ev.get("value", 0)),
                    data=data,
                    chain_id=s.config.chain.chain_id,
                )
                raw = tx.to_rlp()
                with s.repo.transaction():
                    r = s.pool.accept(tx, request_id=req)
                result = {"tx_hash": "0x" + tx.hash().hex(), "status": r["status"],
                          "reason": r["reason"], "sender": sender_addr}
                if ev.get("label"):
                    result["label"] = ev["label"]

            elif etype == "raw":
                raw = bytes.fromhex(ev["raw"][2:])
                r = s.submit_raw(raw, request_id=req)
                result = r

            elif etype == "propose":
                coinbase = ev.get("coinbase")
                if coinbase:
                    _, coinbase_raw = self._resolve_account(coinbase)
                    coinbase = "0x" + coinbase_raw.hex()
                r = s.chain.propose(gas_limit=ev.get("gas_limit"),
                                    coinbase=coinbase, request_id=req)
                result = {"number": r["number"], "hash": r["hash"],
                          "transactions": r["transactions"],
                          "gas_used": r["gas_used"]}

            elif etype == "confirm":
                r = s.chain.confirm(ev.get("block_number"), request_id=req)
                result = {"number": r["number"], "hash": r["hash"],
                          "transactions": r["transactions"],
                          "total_fees": r["total_fees"]}

            elif etype == "discard":
                r = s.chain.discard(request_id=req)
                result = r

            elif etype == "rollback":
                r = s.chain.rollback(int(ev.get("n", 1)), request_id=req)
                result = r

            elif etype == "reap":
                hashes = s.pool.reap_expired(request_id=req)
                result = {"expired": hashes}

            elif etype == "snapshot":
                result = self.snapshot()
                self.snapshots[index] = result

            else:
                raise ReplayAbort(f"unknown event type {etype!r}")

            trace = Trace(index, at, etype, True, result)
        except PoolError as exc:
            trace = Trace(index, at, etype, False,
                          {"error": exc.code, "message": str(exc), "details": exc.details})
            if etype == "tx" and ev.get("label"):
                trace.result["label"] = ev["label"]
        self.traces.append(trace)
        return trace

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        """池状态快照：黄金文件断言的独立事实来源。"""
        s = self.service
        accounts = []
        for row in s.repo.all_accounts():
            accounts.append({"address": to_checksum_address(
                bytes.fromhex(row["address"][2:])),
                "balance": row["balance"], "nonce": row["nonce"]})
        txs = []
        for t in s.repo.list_all(
                ("pending", "queued", "included", "mined", "expired", "replaced", "evicted")):
            txs.append({
                "hash": t.tx_hash,
                "sender": to_checksum_address(bytes.fromhex(t.sender[2:])),
                "nonce": t.nonce,
                "gas_price": str(t.gas_price),
                "status": t.status,
                "reason": t.reason,
                "block_number": t.block_number,
                "position": t.position,
                "replaced_by": t.replaced_by,
            })
        txs.sort(key=lambda x: x["hash"])
        head = s.repo.head_block()
        return {
            "time": s.clock.now(),
            "head_number": head["number"] if head else 0,
            "accounts": sorted(accounts, key=lambda x: x["address"]),
            "transactions": txs,
            "counts": {
                "pending": s.repo.count_status(("pending",)),
                "queued": s.repo.count_status(("queued",)),
                "included": s.repo.count_status(("included",)),
                "mined": s.repo.count_status(("mined",)),
                "expired": s.repo.count_status(("expired",)),
                "replaced": s.repo.count_status(("replaced",)),
                "evicted": s.repo.count_status(("evicted",)),
            },
        }

    def run_file(self, path: str | Path) -> list[dict]:
        events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#")]
        out = []
        for i, ev in enumerate(events):
            trace = self.run_event(i, ev)
            out.append(trace.to_dict())
        return out
