"""两个（及更多）本地模拟客户端。

客户端遵循经典 Jupiter 风格 OT 同步：

* 本地文档 ``text`` 立即应用本地编辑（乐观编辑）；
* 最多一个在途操作 ``inflight``（已发送、未确认），其余编辑进 FIFO ``buffer``；
* 每次 ``sync()``：发送新产生的在途操作 → 拉取服务端增量 → 逐条处理
  （确认自己的操作 / 把别人的操作变换过本地未确认操作后应用）；
* 收到自己在途操作的确认后，buffer 队首自动成为新在途并立即发送；
* 每次编辑（含纯删除）都有客户端内单调的 ``seq``，幂等键固定为
  ``"{client_id}:{seq}"``，重传安全；seq 与操作内部表示解耦
  （只有 insert 在操作内携带 origin 用于同点排序）。

传输方式两种，接口同构：
  :class:`InProcessTransport` 直接调用 :class:`~app.service.OTService`
  （测试与本地模拟默认，无网络）；
  :class:`HTTPTransport` 走 FastAPI/httpx（演示真实 HTTP 调用）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .errors import ClientResetRequired, OTError, StaleBaseline
from .models import Op
from .ot import apply, transform
from .service import OTService


class Transport(Protocol):
    def create_document(self, doc_id: str, text: str) -> None: ...
    def submit(self, doc_id: str, client_id: str, seq: int, base_revision: int,
               op: Op, idem_key: str) -> dict: ...
    def pull(self, doc_id: str, after: int) -> dict: ...
    def get_document(self, doc_id: str) -> dict: ...
    def prune(self, doc_id: str, new_horizon: int) -> dict: ...
    def close(self) -> None: ...


class InProcessTransport:
    """直接调用服务层；返回 dict 与 HTTP 层同构。"""

    def __init__(self, service: OTService):
        self._svc = service

    def create_document(self, doc_id: str, text: str = "") -> None:
        self._svc.create_document(doc_id, text)

    def submit(self, doc_id, client_id, seq, base_revision, op, idem_key) -> dict:
        r = self._svc.submit(doc_id, client_id, seq, base_revision, op, idem_key)
        return {
            "revision": r.revision,
            "head_revision": r.head_revision,
            "rebased": r.rebased,
            "replay": r.replay,
            "text": r.text,
            "op": r.op.to_dict(),
        }

    def pull(self, doc_id, after) -> dict:
        p = self._svc.pull(doc_id, after)
        return {
            "head_revision": p.head_revision,
            "pruned_horizon": p.horizon,
            "has_more": p.has_more,
            "text": p.text,
            "operations": [
                {
                    "revision": s.revision,
                    "client_id": s.client_id,
                    "client_seq": s.client_seq,
                    "components": s.op.to_dict()["components"],
                }
                for s in p.ops
            ],
        }

    def get_document(self, doc_id) -> dict:
        return self._svc.get_document(doc_id)

    def prune(self, doc_id, new_horizon) -> dict:
        return self._svc.prune(doc_id, new_horizon)

    def close(self) -> None:
        pass


class HTTPTransport:
    """走 FastAPI 的真实 HTTP 传输（供手动演示）。"""

    def __init__(self, base_url: str):
        self._client = httpx.Client(base_url=base_url, timeout=10.0)

    @staticmethod
    def _raise_for_error(resp: httpx.Response):
        if resp.status_code >= 400:
            data = resp.json().get("error", {})
            from . import errors as E
            code = data.get("code")
            cls = next(
                (c for c in vars(E).values()
                 if isinstance(c, type) and issubclass(c, OTError)
                 and getattr(c, "code", None) == code),
                OTError,
            )
            raise cls(data.get("message", ""), details=data.get("details", {}))

    def create_document(self, doc_id, text="") -> None:
        resp = self._client.post("/documents", json={"doc_id": doc_id, "text": text})
        self._raise_for_error(resp)

    def submit(self, doc_id, client_id, seq, base_revision, op, idem_key) -> dict:
        resp = self._client.post(
            f"/documents/{doc_id}/ops",
            json={
                "client_id": client_id,
                "client_seq": seq,
                "base_revision": base_revision,
                "components": op.to_dict()["components"],
            },
            headers={"Idempotency-Key": idem_key},
        )
        self._raise_for_error(resp)
        return resp.json()

    def pull(self, doc_id, after) -> dict:
        resp = self._client.get(f"/documents/{doc_id}/ops", params={"after": after})
        self._raise_for_error(resp)
        return resp.json()

    def get_document(self, doc_id) -> dict:
        resp = self._client.get(f"/documents/{doc_id}")
        self._raise_for_error(resp)
        return resp.json()

    def prune(self, doc_id, new_horizon) -> dict:
        resp = self._client.post(
            f"/documents/{doc_id}/prune", params={"new_horizon": new_horizon}
        )
        self._raise_for_error(resp)
        return resp.json()

    def close(self) -> None:
        self._client.close()


@dataclass
class _Pending:
    seq: int              # 客户端内单调序号（幂等/确认用）
    op: Op                # 本地形态：随吸收远端操作而演进
    base_revision: int    # 入队/提升时的服务端版本（首次发送后冻结）
    send_op: Op | None = None    # 首次发送形态（重传必须原样重发）
    send_base: int | None = None


@dataclass
class LocalClient:
    """单个本地模拟客户端。

    不变量：``text`` 始终反映"本地编辑全部已应用"的视图；``inflight`` 与
    ``buffer`` 描述本地视图相对服务端已确认视图的差异。
    """

    client_id: str
    doc_id: str
    transport: Transport
    text: str = ""
    revision: int = 0
    inflight: _Pending | None = None
    buffer: list[_Pending] = field(default_factory=list)
    _seq: int = 0

    # ------------------------------------------------------------ 编辑
    def insert(self, pos: int, text: str) -> int:
        seq = self._bump_seq()
        op = Op.insert_at(pos, text, (self.client_id, seq), len(self.text))
        self._enqueue_and_apply(seq, op)
        return seq

    def delete(self, pos: int, n: int) -> int:
        seq = self._bump_seq()
        op = Op.delete_range(pos, n, len(self.text))
        self._enqueue_and_apply(seq, op)
        return seq

    def _bump_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _enqueue_and_apply(self, seq: int, op: Op) -> None:
        self.text = apply(op, self.text)
        pending = _Pending(seq=seq, op=op, base_revision=self.revision)
        if self.inflight is None:
            self.inflight = pending
        else:
            self.buffer.append(pending)

    # ------------------------------------------------------------ 同步
    SYNC_ROUNDS_LIMIT = 64

    def sync(self) -> dict:
        sent = 0
        received = 0
        for _ in range(self.SYNC_ROUNDS_LIMIT):
            # 1) 在途操作发送。重传必须使用首次发送的形态与基线
            #    （本地形态会随吸收远端操作而演进，不能用于重发）。
            submit_present = self.inflight is not None
            if submit_present:
                p = self.inflight
                if p.send_op is None:  # 首次发送：冻结
                    p.send_op = p.op
                    p.send_base = p.base_revision
                try:
                    self.transport.submit(
                        self.doc_id, self.client_id, p.seq,
                        p.send_base, p.send_op, f"{self.client_id}:{p.seq}",
                    )
                except StaleBaseline:
                    # 发送基线已被裁剪：必须重建基线（旧历史无法重放）
                    raise ClientResetRequired(
                        "submit base pruned; rebuild baseline",
                    )
                sent += 1

            # 2) 拉取自上次确认版本后的增量
            pulled = self.transport.pull(self.doc_id, self.revision)
            if self.revision < pulled["pruned_horizon"] and self._has_pending():
                # 基线已裁剪且仍有未确认编辑：OT 无法续传，必须重置
                raise ClientResetRequired(
                    "baseline pruned with unacknowledged edits; rebuild required",
                )

            acked = False
            entries = pulled["operations"]
            for entry in entries:
                remote = Op.from_dict({"components": entry["components"]})
                is_own_ack = (
                    entry["client_id"] == self.client_id
                    and self.inflight is not None
                    and entry["client_seq"] == self.inflight.seq
                )
                if is_own_ack:
                    self.inflight = None
                    acked = True
                else:
                    self._absorb_remote(remote)
                    received += 1
                self.revision = entry["revision"]

            # 3) 队首提升为新在途（下一轮以当前版本为基线首次发送）
            if acked and self.buffer:
                nxt = self.buffer.pop(0)
                self.inflight = _Pending(
                    seq=nxt.seq, op=nxt.op, base_revision=self.revision,
                )

            head = pulled["head_revision"]
            if not acked and not submit_present and not entries and self.revision == head:
                break  # 静止
        else:
            raise ClientResetRequired("sync did not quiesce within round limit")

        return {
            "client_id": self.client_id,
            "revision": self.revision,
            "text": self.text,
            "inflight": self.inflight is not None,
            "buffered": len(self.buffer),
            "sent": sent,
            "received": received,
        }

    def _has_pending(self) -> bool:
        return self.inflight is not None or bool(self.buffer)

    def _absorb_remote(self, remote: Op) -> None:
        """把一条服务端操作迁移到本地视图：依次穿过 inflight 与 buffer。"""
        transformed_remote = remote
        new_own: list[Op] = []
        own_ops = (
            ([self.inflight.op] if self.inflight is not None else [])
            + [p.op for p in self.buffer]
        )
        for own in own_ops:
            transformed_remote, own_rebased = transform(transformed_remote, own)
            new_own.append(own_rebased)
        self.text = apply(transformed_remote, self.text)

        if self.inflight is not None:
            self.inflight = _Pending(
                seq=self.inflight.seq,
                op=new_own[0],
                base_revision=self.inflight.base_revision,
            )
            for idx, op in enumerate(new_own[1:]):
                self.buffer[idx] = _Pending(
                    seq=self.buffer[idx].seq,
                    op=op,
                    base_revision=self.buffer[idx].base_revision,
                )
        else:
            for idx, op in enumerate(new_own):
                self.buffer[idx] = _Pending(
                    seq=self.buffer[idx].seq,
                    op=op,
                    base_revision=self.buffer[idx].base_revision,
                )

    # ------------------------------------------------------------ 加入
    def join(self) -> dict:
        """首次打开一个可能已存在（且可能已有内容）的文档：取全文快照。

        与 :meth:`rebuild_baseline` 的区别仅在语义：join 用于初始加入，
        不表示丢弃未确认编辑。
        """
        doc = self.transport.get_document(self.doc_id)
        self.text = doc["text"]
        self.revision = doc["head_revision"]
        self.inflight = None
        self.buffer.clear()
        return {"client_id": self.client_id, "revision": self.revision,
                "text": self.text}

    # ------------------------------------------------------------ 重置
    def rebuild_baseline(self) -> dict:
        """旧基线失效时：丢弃未确认编辑，以服务端当前全文与 head 重建。"""
        doc = self.transport.get_document(self.doc_id)
        self.text = doc["text"]
        self.revision = doc["head_revision"]
        self.inflight = None
        self.buffer.clear()
        return {
            "client_id": self.client_id,
            "revision": self.revision,
            "text": self.text,
            "dropped_edits": True,
        }
