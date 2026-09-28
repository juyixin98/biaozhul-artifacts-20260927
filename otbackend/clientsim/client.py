"""模拟 OT 客户端状态机。

约定
----
* 一次 flush 单位 = 一条消息 = 一个 ``client_op_id``；消息内所有插入
  在发送前统一盖成同一个 ``(client_id, client_op_id)``，与服务端
  “同消息单来源”校验一致；
* 在途消息未确认期间的新编辑进入 ``queued``（各自先带临时序号做
  compose，回声确认后整体重盖一个*新*消息号再发送）；
* 与服务端的并发同点插入裁决使用完全相同的键
  ``(client_id, client_op_id, text, 端序兜底)``，因此客户端本地
  重定基与服务端变换必然收敛到同一文本。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import StateConflict
from ..textmodel import Comp, apply_stream
from ..transform import compose, xform


@dataclass(slots=True)
class LocalEdit:
    ops: list[Comp]
    label: str = ""


@dataclass(slots=True)
class _Outstanding:
    ops: list[Comp]
    base_rev: int
    client_op_id: int


class SimClient:
    def __init__(self, client_id: str, transport, doc_id: str,
                 start_text: str = "", start_rev: int = 0, create: bool = False):
        self.client_id = client_id
        self.t = transport
        self.doc_id = doc_id
        self.text = start_text
        self.server_rev = start_rev
        self.outstanding: _Outstanding | None = None
        self.queued: list[Comp] = []
        self._next_op_id = 1
        self.rebuilds = 0  # 因历史裁剪而重建基线的次数（诊断用）
        if create:
            resp = self.t.create_document(doc_id, start_text)
            self.text = resp.get("text", start_text)
            self.server_rev = resp.get("rev", 0)

    # -------------------------------------------------- 生命周期

    def join(self) -> "SimClient":
        resp = self.t.catchup(self.doc_id)
        self.text = resp["text"]
        self.server_rev = resp["rev"]
        self.outstanding = None
        self.queued = []
        return self

    # -------------------------------------------------- 本地编辑

    def local_insert(self, pos: int, text: str) -> LocalEdit:
        edit = LocalEdit([Comp.ins(pos, text)], label=f"ins@{pos}:{text!r}")
        self.apply_local(edit)
        return edit

    def local_delete(self, pos: int, length: int) -> LocalEdit:
        edit = LocalEdit([Comp.del_(pos, length)], label=f"del@{pos}+{length}")
        self.apply_local(edit)
        return edit

    def apply_local(self, edit: LocalEdit) -> None:
        self.text = apply_stream(self.text, edit.ops)
        tmp_id = self._alloc_op_id()
        stamped = self._stamp(edit.ops, tmp_id)
        if self.outstanding is None:
            msg_id = self._alloc_op_id()
            self.outstanding = _Outstanding(
                self._stamp(edit.ops, msg_id), self.server_rev, msg_id)
        else:
            # 在途期间：临时序号只用于 compose 保持插入相对次序，
            # 真正发送时会重盖新消息号。
            self.queued = compose(self.queued, stamped) if self.queued else stamped

    def _alloc_op_id(self) -> int:
        v = self._next_op_id
        self._next_op_id += 1
        return v

    def _stamp(self, ops: list[Comp], msg_id: int) -> list[Comp]:
        return [c.stamp(self.client_id, msg_id) if c.is_ins else c for c in ops]

    def _promote_queued(self, base_rev: int) -> _Outstanding:
        msg_id = self._alloc_op_id()
        out = _Outstanding(self._stamp(self.queued, msg_id), base_rev, msg_id)
        self.queued = []
        return out

    # -------------------------------------------------- 发送 / 接收

    def flush(self) -> dict:
        if self.outstanding is None and self.queued:
            self.outstanding = self._promote_queued(self.server_rev)
        if self.outstanding is None:
            return {"sent": False}
        o = self.outstanding
        payload = [c.to_dict() for c in o.ops]  # 携带来源身份，服务端校验一致
        try:
            resp = self.t.submit(self.doc_id, o.base_rev, self.client_id,
                                 o.client_op_id, payload)
        except StateConflict as e:
            if e.reason == "BASELINE_TRIMMED":
                return self._rebuild_after_trim()
            raise
        return {"sent": True, "response": resp,
                "label": f"{self.client_id}#{o.client_op_id}"}

    def _rebuild_after_trim(self) -> dict:
        resp = self.t.catchup(self.doc_id)
        self.text = resp["text"]
        self.server_rev = resp["rev"]
        self.outstanding = None
        self.queued = []
        self.rebuilds += 1
        return {"sent": False, "rebuilt": True, "rev": self.server_rev}

    def poll(self) -> list[dict]:
        try:
            bundle = self.t.history(self.doc_id, self.server_rev)
        except StateConflict as e:
            if e.reason == "BASELINE_TRIMMED":
                self._rebuild_after_trim()
                return [{"rebuilt": True, "rev": self.server_rev}]
            raise
        applied = []
        for rv in bundle["revisions"]:
            if rv["rev"] > self.server_rev:
                applied.append(self._absorb(rv))
        return applied

    def _absorb(self, rv: dict) -> dict:
        rev_ops = [Comp.from_dict(d) for d in rv["ops"]]
        own = (rv["client_id"] == self.client_id
               and self.outstanding is not None
               and rv["client_op_id"] == self.outstanding.client_op_id)

        if own:
            old = self.outstanding
            self.outstanding = None
            if self.queued:
                self.outstanding = self._promote_queued(rv["rev"])
            self.server_rev = rv["rev"]
            return {"rev": rv["rev"], "kind": "echo", "op_id": old.client_op_id}

        # 他人修订：依次对 outstanding、queued 重定基
        remote = rev_ops
        if self.outstanding is not None:
            remote, new_out = xform(remote, self.outstanding.ops)
            self.outstanding = _Outstanding(
                new_out, self.outstanding.base_rev, self.outstanding.client_op_id)
        if self.queued:
            remote, new_queued = xform(remote, self.queued)
            self.queued = new_queued
        self.text = apply_stream(self.text, remote)
        self.server_rev = rv["rev"]
        return {"rev": rv["rev"], "kind": "remote",
                "from": rv["client_id"],
                "applied": [c.to_dict() for c in remote]}

    # -------------------------------------------------- 诊断

    def snapshot(self) -> dict:
        return {
            "client_id": self.client_id,
            "server_rev": self.server_rev,
            "text": self.text,
            "outstanding": None if self.outstanding is None else {
                "base_rev": self.outstanding.base_rev,
                "op_id": self.outstanding.client_op_id,
                "ops": [c.to_dict() for c in self.outstanding.ops],
            },
            "queued": [c.to_dict() for c in self.queued],
            "rebuilds": self.rebuilds,
        }
