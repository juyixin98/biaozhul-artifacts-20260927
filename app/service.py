"""服务层：提交/拉取/裁剪/查询的事务编排与错误分类。

线程模型：一把进程内可重入锁串行化所有写入；存储层用单条 SQLite 连接。
这对本地模拟与单机部署足够（SQLite 本身不提供多写者协作）。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass

from .config import Settings, settings as default_settings
from .errors import (
    DocumentNotFound,
    DocumentTooLarge,
    DuplicateRequest,
    EmptyInsert,
    MalformedOperation,
    OperationTooLarge,
    RevisionAhead,
    StaleBaseline,
    TransformInvariant,
)
from .models import Component, Op
from .ot import apply, transform
from .storage import StoredOp, Storage


def _request_sha(client_id: str, client_seq: int, base_revision: int, op: Op) -> str:
    payload = json.dumps(
        {
            "client_id": client_id,
            "client_seq": client_seq,
            "base_revision": base_revision,
            "op": op.to_dict(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SubmitResult:
    revision: int
    head_revision: int
    text: str
    op: Op                # 实际落库（变换后）的操作
    rebased: bool         # 是否经历了并发变换
    base_revision: int
    replay: bool = False  # 是否为重复提交的幂等重放


@dataclass(frozen=True, slots=True)
class PullResult:
    head_revision: int
    horizon: int
    text: str
    ops: list[StoredOp]
    has_more: bool


class OTService:
    def __init__(self, storage: Storage, settings: Settings = default_settings):
        self._db = storage
        self._cfg = settings
        self._lock = threading.RLock()
        self._faults: dict[str, int] = {}  # doc_id -> 剩余注入次数

    # ------------------------------------------------------------- 诊断
    def arm_fault(self, doc_id: str, times: int = 1) -> None:
        """安排 ``doc_id`` 的下 ``times`` 次提交抛出 TransformInvariant。"""
        self._faults[doc_id] = self._faults.get(doc_id, 0) + times

    # ------------------------------------------------------------- 文档
    def create_document(self, doc_id: str, text: str = "") -> None:
        if not isinstance(doc_id, str) or not doc_id:
            raise MalformedOperation("doc_id must be non-empty str")
        if not isinstance(text, str):
            raise MalformedOperation("initial text must be str")
        if len(text) > self._cfg.max_doc_chars:
            raise DocumentTooLarge(
                f"initial text {len(text)} > limit {self._cfg.max_doc_chars}",
            )
        with self._lock:
            self._db.create_document(doc_id, text)

    def _require_doc(self, doc_id: str):
        row = self._db.get_document_row(doc_id)
        if row is None:
            raise DocumentNotFound(doc_id)
        return row

    def get_document(self, doc_id: str) -> dict:
        with self._lock:
            row = self._require_doc(doc_id)
            return {
                "doc_id": row["doc_id"],
                "text": row["text"],
                "head_revision": row["head_revision"],
                "pruned_horizon": row["pruned_horizon"],
                "length": len(row["text"]),
            }

    def text_at(self, doc_id: str, revision: int) -> str:
        """返回指定版本的全文（裁剪水位之上的版本可由快照前向重放得到）。

        供客户端在历史裁剪后重建基线使用；版本低于水位抛 StaleBaseline。
        """
        with self._lock:
            row = self._require_doc(doc_id)
            horizon, head = row["pruned_horizon"], row["head_revision"]
            if revision < horizon:
                raise StaleBaseline(revision, horizon)
            if revision > head:
                raise RevisionAhead(revision, head)
            if revision == head:
                return row["text"]
            return self._db.rebuild_text(doc_id, revision)

    def list_documents(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.list_documents()]

    # ------------------------------------------------------------- 提交
    def submit(
        self,
        doc_id: str,
        client_id: str,
        client_seq: int,
        base_revision: int,
        op: Op,
        idem_key: str | None = None,
    ) -> SubmitResult:
        # ---- 纯输入校验（锁外即可判定） ----
        if not isinstance(client_id, str) or not client_id:
            raise MalformedOperation("client_id must be non-empty str")
        if not isinstance(client_seq, int) or isinstance(client_seq, bool) or client_seq <= 0:
            raise MalformedOperation("client_seq must be positive int")
        if (
            not isinstance(base_revision, int)
            or isinstance(base_revision, bool)
            or base_revision < 0
        ):
            raise MalformedOperation("base_revision must be non-negative int")
        if not isinstance(op, Op):
            raise MalformedOperation("op required")
        if op.is_noop():
            raise EmptyInsert("client submit must contain an insert or delete")
        # 操作里所有插入必须来自声明的 (client_id, client_seq)——禁止冒认来源，
        # 否则同点插入排序会被伪造的来源键破坏。
        for origin in op.origins():
            if origin != (client_id, client_seq):
                raise MalformedOperation(
                    "every insert origin must equal (client_id, client_seq)",
                )
        if op.inserted > self._cfg.max_op_chars or op.deleted > self._cfg.max_op_chars:
            raise OperationTooLarge(
                f"op edits {max(op.inserted, op.deleted)} chars,"
                f" limit {self._cfg.max_op_chars}",
            )

        sha = _request_sha(client_id, client_seq, base_revision, op)

        with self._lock:
            row = self._require_doc(doc_id)
            head = row["head_revision"]
            horizon = row["pruned_horizon"]
            head_text = row["text"]

            # 诊断注入：计算失败类别（只在提交核心路径触发）
            remaining = self._faults.get(doc_id, 0)
            if remaining > 0:
                self._faults[doc_id] = remaining - 1
                raise TransformInvariant("injected fault during submit")

            # ---- 幂等键：先查（不依赖版本水位） ----
            if idem_key is not None:
                prev = self._db.get_idem(doc_id, idem_key)
                if prev is not None:
                    if prev["request_sha"] != sha:
                        raise DuplicateRequest(
                            "idempotency key reused with a different request body",
                        )
                    sop = self._db.get_op(doc_id, prev["revision"])
                    if sop is None:
                        # 首次结果所在操作已被裁剪：旧基线，客户端重建即可
                        raise StaleBaseline(prev["revision"], horizon)
                    return SubmitResult(
                        revision=sop.revision,
                        head_revision=head,
                        text=head_text,
                        op=sop.op,
                        rebased=sop.base_revision != base_revision,
                        base_revision=base_revision,
                        replay=True,
                    )

            # ---- 同客户端 seq 的天然去重（重复提交） ----
            dup = self._db.get_client_seq_op(doc_id, client_id, client_seq)
            if dup is not None:
                dup_sha = _request_sha(client_id, client_seq, dup.base_revision, dup.op)
                # 同 seq 同请求视为重传；base_revision/op 不同则是 seq 复用冲突
                if dup_sha == sha:
                    return SubmitResult(
                        revision=dup.revision,
                        head_revision=head,
                        text=head_text,
                        op=dup.op,
                        rebased=dup.base_revision != base_revision,
                        base_revision=base_revision,
                        replay=True,
                    )
                raise DuplicateRequest(
                    f"client {client_id} reused seq {client_seq} with different payload",
                )

            # ---- 版本水位检查（状态冲突） ----
            if base_revision < horizon:
                raise StaleBaseline(base_revision, horizon)
            if base_revision > head:
                raise RevisionAhead(base_revision, head)

            # 操作基线长度必须与该版本文档长度一致（足以拒绝所有越界删除）
            if op.base_len != self._length_at(doc_id, horizon, head, base_revision):
                raise MalformedOperation(
                    "op base length does not match document length at base_revision",
                )

            # ---- 并发重放：把操作依次 transform 穿过 base+1..head 已落库
            # 的（变换后）操作。每个客户端采用"最多一个在途操作"的同步纪律，
            # 因此同一并发区间里每个客户端至多贡献一个操作；逐对变换满足 TP1
            # 收敛，同点并发插入按 origin 稳定排序。
            rebased = op
            rebased_flag = base_revision != head
            if rebased_flag:
                stored_concurrent = self._db.get_ops(
                    doc_id, base_revision, head - base_revision
                )
                if len(stored_concurrent) != head - base_revision:
                    raise TransformInvariant("missing concurrent ops after horizon check")
                for landed in stored_concurrent:
                    rebased, _ = transform(rebased, landed.op)
            # 变换后可能退化为空（要删的字符已全部被并发方删除）。这是合法
            # 的"编辑意图已被满足"：归一化为覆盖整个 head 的 no-op 标记版本，
            # 客户端据此正常收到确认，文本不变。
            if rebased.is_noop() and rebased.base_len != len(head_text):
                rebased = Op.build([Component.retain(len(head_text))])

            # ---- 在 head 文本上落地；配额在写入前判定 ----
            try:
                new_text = apply(rebased, head_text)
            except MalformedOperation:
                raise TransformInvariant("rebased op failed to apply at head")
            if len(new_text) > self._cfg.max_doc_chars:
                raise DocumentTooLarge(
                    f"document would be {len(new_text)} chars,"
                    f" limit {self._cfg.max_doc_chars}",
                )

            revision = head + 1
            stored = StoredOp(
                doc_id=doc_id,
                revision=revision,
                client_id=client_id,
                client_seq=client_seq,
                base_revision=base_revision,
                op=rebased,
                idem_key=idem_key,
                created_at=time.time(),
            )
            self._db.commit_revision(doc_id, stored, new_text, idem_key, sha)
            return SubmitResult(
                revision=revision,
                head_revision=revision,
                text=new_text,
                op=rebased,
                rebased=rebased_flag,
                base_revision=base_revision,
            )

    def _length_at(self, doc_id: str, horizon: int, head: int, revision: int) -> int:
        if revision == head:
            return len(self._require_doc(doc_id)["text"])
        sop = self._db.get_op(doc_id, revision + 1)
        # 注意：op r 落库后的长度 = op r 的 target_len；要取版本 r 的长度，
        # 等价于"在 r 之上的第一个操作"的基线长度。
        if sop is not None:
            return sop.op.base_len
        # revision == 最近水位：读快照
        text = self._db.get_snapshot(doc_id, revision)
        if text is None:
            raise TransformInvariant(f"cannot resolve document length at rev {revision}")
        return len(text)

    # ------------------------------------------------------------- 拉取
    def pull(self, doc_id: str, after_revision: int, limit: int | None = None) -> PullResult:
        if not isinstance(after_revision, int) or isinstance(after_revision, bool) or after_revision < 0:
            raise MalformedOperation("after_revision must be non-negative int")
        batch_limit = min(limit or self._cfg.max_pull_batch, self._cfg.max_pull_batch)
        with self._lock:
            row = self._require_doc(doc_id)
            head = row["head_revision"]
            horizon = row["pruned_horizon"]
            if after_revision < horizon:
                raise StaleBaseline(after_revision, horizon)
            if after_revision > head:
                raise RevisionAhead(after_revision, head)
            ops = self._db.get_ops(doc_id, after_revision, batch_limit + 1)
            has_more = len(ops) > batch_limit
            ops = ops[:batch_limit]
            return PullResult(
                head_revision=head,
                horizon=horizon,
                text=row["text"],
                ops=ops,
                has_more=has_more,
            )

    # ------------------------------------------------------------- 裁剪
    def prune(self, doc_id: str, new_horizon: int) -> dict:
        """把历史裁剪到 ``new_horizon``：该版本及更早的操作从日志移除，
        并写入该版本全文快照作为新的重建基线。
        """
        if not isinstance(new_horizon, int) or isinstance(new_horizon, bool) or new_horizon < 0:
            raise MalformedOperation("new_horizon must be non-negative int")
        with self._lock:
            row = self._require_doc(doc_id)
            head = row["head_revision"]
            horizon = row["pruned_horizon"]
            if new_horizon <= horizon:
                raise MalformedOperation(
                    f"new_horizon {new_horizon} must exceed current horizon {horizon}",
                )
            if new_horizon > head:
                raise RevisionAhead(new_horizon, head)
            snapshot_text = self._db.rebuild_text(doc_id, new_horizon)
            self._db.prune(doc_id, new_horizon, snapshot_text)
            retained = self._db.count_ops(doc_id)
            return {
                "doc_id": doc_id,
                "pruned_horizon": new_horizon,
                "head_revision": head,
                "retained_ops": retained,
                "snapshot_length": len(snapshot_text),
            }

    # ------------------------------------------------------------- 诊断
    def diagnostics(self) -> dict:
        with self._lock:
            docs = []
            for d in self.list_documents():
                doc_id = d["doc_id"]
                row = self._require_doc(doc_id)
                ops_count = self._db.count_ops(doc_id)
                docs.append(
                    {
                        "doc_id": doc_id,
                        "head_revision": d["head_revision"],
                        "pruned_horizon": d["pruned_horizon"],
                        "retained_ops": ops_count,
                        "char_length": d["char_len"],
                        "snapshot_revisions": self._db.latest_snapshot_revision(doc_id),
                        "pending_faults": self._faults.get(doc_id, 0),
                    }
                )
            return {
                "db_head_revision_max": max((d["head_revision"] for d in docs), default=0),
                "documents": docs,
                "limits": {
                    "max_doc_chars": self._cfg.max_doc_chars,
                    "max_op_chars": self._cfg.max_op_chars,
                    "max_pull_batch": self._cfg.max_pull_batch,
                },
                "fault_injection_armed": bool(self._faults),
            }
