"""状态服务：链内核 + 索引存储 + 检查点签名的事务编排。

职责边界：
* 树结构与证明数学在 app.core；持久化在 app.storage；
* 本类只做“一个版本 = 一个事务”的编排、幂等、签名与审计索引维护。
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from app.coding.keys import normalize_key
from app.coding.serialization import canonical_json
from app.coding.signing import (
    Ed25519PrivateKey,
    sign_checkpoint,
)
from app.core.batch import (
    apply_batch,
    canonical_batch_id,
    normalize_batch,
)
from app.core.proof import LogicalProof
from app.core.smt import SparseMerkleTree
from app.storage.sqlite_store import SqliteStore
from .proof_envelope import proof_to_envelope

GENESIS_BATCH_ID = "genesis-empty-root"


@dataclass(frozen=True)
class UpdateResult:
    version: int
    root: bytes
    parent_root: bytes
    batch_id: str
    changed: int
    idempotent_replay: bool


class StateService:
    def __init__(self, store: SqliteStore, tree: SparseMerkleTree,
                 signer: Ed25519PrivateKey) -> None:
        self.store = store
        self.tree = tree
        self.params = tree.params
        self.signer = signer
        self._init_genesis()

    def _init_genesis(self) -> None:
        root0 = self.params.empty_root
        sig = sign_checkpoint(self.signer, 0, root0.hex(), None, GENESIS_BATCH_ID)
        self.store.ensure_genesis(root0, GENESIS_BATCH_ID, sig)

    # ---------- 读 ----------

    def current_version(self) -> int:
        return self.store.current_version()

    def current_root(self) -> bytes:
        return self.store.latest_checkpoint()["root"]

    def root_for_version(self, version: int) -> bytes | None:
        return self.store.root_for_version(version)

    def get_value(self, key: bytes) -> bytes | None:
        key = normalize_key(key, self.params.key_len)
        root = self.current_root()
        return self.tree.get_value(root, key)

    def proof_for_key(self, key: bytes, version: int | None = None) -> dict:
        """生成证明信封。version=None 取最新；旧版本只要节点未被物理删除
        （内容寻址、永不删除）即可生成并由历史根核验。"""
        key = normalize_key(key, self.params.key_len)
        target_version = self.current_version() if version is None else version
        root = self.store.root_for_version(target_version)
        if root is None:
            raise KeyError(f"未知版本: {target_version}")
        proof = self.tree.prove(root, key)
        return proof_to_envelope(proof, self.params)

    # ---------- 写 ----------

    def apply_updates(
        self,
        items: list[tuple[bytes, bytes | None]],
        idem_key: str | None = None,
    ) -> UpdateResult:
        updates = normalize_batch(items, self.params.key_len)
        batch_id = canonical_batch_id(updates)
        payload_hash = hashlib.sha256(canonical_json([u.to_journal() for u in updates])).hexdigest()

        # 幂等：同键同载荷 -> 回放旧结果；同键异载荷 -> 冲突
        if idem_key is not None:
            existing = self.store.idempotency_lookup(idem_key)
            if existing is not None:
                if existing["payload_hash"] != payload_hash:
                    raise IdempotencyConflict(
                        idem_key=idem_key,
                        existing_version=existing["version"],
                        existing_batch_id=existing["batch_id"],
                    )
                cp = self.store.checkpoint(existing["version"])
                return UpdateResult(
                    version=cp["version"], root=cp["root"], parent_root=cp["parent_root"],
                    batch_id=cp["batch_id"], changed=cp["changed"], idempotent_replay=True,
                )

        with self.store.transaction():
            version = self.store.current_version() + 1
            parent_root = self.store.latest_checkpoint()["root"]
            new_root, changed = apply_batch(self.tree, parent_root, updates)
            signature = sign_checkpoint(
                self.signer, version, new_root.hex(), parent_root.hex(), batch_id
            )
            self.store.add_version(version, new_root, parent_root, batch_id, signature, changed)
            for seq, u in enumerate(updates):
                self.store.append_journal(version, batch_id, seq, u.key, u.value)
                if u.value is None:
                    self.store.delete_index(u.key)
                else:
                    self.store.upsert_index(u.key, u.value, version)
            if idem_key is not None:
                self.store.idempotency_store(idem_key, batch_id, version, payload_hash)

        return UpdateResult(
            version=version, root=new_root, parent_root=parent_root,
            batch_id=batch_id, changed=changed, idempotent_replay=False,
        )

    def build_logical_proof(self, root: bytes, key: bytes) -> LogicalProof:
        return self.tree.prove(root, normalize_key(key, self.params.key_len))

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex


class IdempotencyConflict(Exception):
    def __init__(self, idem_key: str, existing_version: int, existing_batch_id: str) -> None:
        self.idem_key = idem_key
        self.existing_version = existing_version
        self.existing_batch_id = existing_batch_id
        super().__init__(
            f"幂等键 {idem_key!r} 已用于版本 {existing_version}（批次 {existing_batch_id}）"
        )
