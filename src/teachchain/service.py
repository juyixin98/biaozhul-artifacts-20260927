"""链服务：把内存内核与 SQLite 索引存储接起来。

* :meth:`submit` 执行一笔交易：成功（含 VM 失败扣费）落收据；准入拒绝向上抛
  :class:`Rejected`，不写任何索引；
* :meth:`bootstrap` 从已有数据库重建内存状态（代码 + 存储 + nonce + 余额）；
* 状态的权威重建路径是“重放全部信封”（见 :mod:`teachchain.replay`）；
  本模块加载索引视图，随后可调用 replay 做完整性校验。
"""

from __future__ import annotations

import base64
from typing import Any

from .errors import Rejected
from .kernel import ChainState, Kernel
from .storage import IndexStore
from .version import ENGINE_VERSION


class ChainService:
    def __init__(self, store: IndexStore, diag=None) -> None:
        self.store = store
        self.state = ChainState()
        self.kernel = Kernel(self.state, diag=diag)
        self.diag = diag
        self.bootstrap()

    def bootstrap(self) -> None:
        """从索引存储重建内核内存状态。"""
        st = self.state = ChainState()
        self.kernel = Kernel(st, diag=self.diag)
        for address, code_b64 in self.store.contracts().items():
            st.codes[address] = base64.b64decode(code_b64)
        # 账户/存储/nonce 取索引视图（权威重建路径是 replay.replay_store）
        rows = self.store.conn.execute("SELECT address, nonce, balance FROM accounts")
        for r in rows:
            st.nonces[r["address"]] = int(r["nonce"])
            st.balances[r["address"]] = int(r["balance"])
        rows = self.store.conn.execute("SELECT address, slot, value FROM storage_slots")
        for r in rows:
            st.storage[(r["address"], int(r["slot"]))] = int(r["value"])
        st.height = self.store.max_height()

    def seed(self, address: str, amount: int) -> None:
        self.kernel.credit(address, amount)
        self.store.seed_account(address, amount)

    def submit(self, envelope: dict[str, Any]) -> dict[str, Any]:
        receipt = self.kernel.apply_tx(envelope)  # Rejected 直接上抛
        data = receipt.data
        self.store.save_accepted(envelope, data)
        # 结算后的真实余额/nonce 回写索引视图
        sender = data["from"]
        self.store.set_account(
            sender,
            self.state.nonces.get(sender, 0),
            self.state.balances.get(sender, 0),
        )
        return dict(data)

    def get_receipt(self, height: int | None = None, tx_hash: str | None = None):
        if tx_hash:
            return self.store.get_receipt_by_tx(tx_hash)
        return self.store.get_receipt(height if height is not None else self.store.max_height())

    def engine_version(self) -> str:
        return ENGINE_VERSION
