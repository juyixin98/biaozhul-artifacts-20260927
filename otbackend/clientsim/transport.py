"""客户端与服务端之间的传输抽象。

* :class:`InProcessTransport` —— 直接调用 OTService，无网络；
* :class:`ScriptedNetwork` —— 包在服务外面，把提交请求排队，按
  演示脚本指定的顺序投递，用于构造“不同接收顺序”的确定性场景；
* :class:`HttpTransport` —— 用 httpx 访问真正运行的 FastAPI 服务。
"""

from __future__ import annotations

from typing import Any

from ..errors import OTError


class Transport:
    def create_document(self, doc_id: str, initial_text: str = "") -> dict: ...
    def submit(self, doc_id: str, base_rev: int, client_id: str,
               client_op_id: int, ops: list[dict]) -> dict: ...
    def history(self, doc_id: str, since: int) -> dict: ...
    def catchup(self, doc_id: str) -> dict: ...


class InProcessTransport(Transport):
    def __init__(self, service, doc_id: str):
        self.service = service
        self.doc_id = doc_id

    def create_document(self, doc_id, initial_text=""):
        v = self.service.create_document(doc_id, initial_text)
        return {"doc_id": v.doc_id, "rev": v.rev, "text": v.text,
                "baseline_rev": v.baseline_rev}

    def submit(self, doc_id, base_rev, client_id, client_op_id, ops):
        ack = self.service.submit(doc_id, base_rev, client_id, client_op_id, ops)
        return {"rev": ack.rev, "head_rev": ack.head_rev,
                "ops": [c.to_dict() for c in ack.ops], "text": ack.text}

    def history(self, doc_id, since):
        revs = self.service.history(doc_id, since=since, limit=10_000)
        return {
            "baseline_rev": self.service.repo.baseline_rev(doc_id),
            "head_rev": self.service.repo.head_rev(doc_id),
            "revisions": [
                {"rev": r.rev, "client_id": r.client_id,
                 "client_op_id": r.client_op_id, "base_rev": r.base_rev,
                 "length_before": r.length_before, "length_after": r.length_after,
                 "checksum": r.checksum, "ops": [c.to_dict() for c in r.ops]}
                for r in revs
            ],
        }

    def catchup(self, doc_id):
        v = self.service.catchup(doc_id)
        return {"rev": v.rev, "baseline_rev": v.baseline_rev, "text": v.text}


class ScriptedNetwork(Transport):
    """把提交请求缓存在内存队列，由演示脚本显式投递。

    历史读取始终直接反映服务端当前状态（投递后即可 poll 到）。
    每个客户端用 :meth:`view` 得到自己的传输句柄；所有句柄共享
    同一个待发队列。
    """

    def __init__(self, service):
        self.service = service
        self.pending: list[dict[str, Any]] = []
        self.delivered: list[dict[str, Any]] = []
        self._doc_id: str | None = None

    def attach(self, doc_id: str) -> "ScriptedNetwork":
        self._doc_id = doc_id
        return self

    def view(self) -> "ScriptedNetwork":
        return self

    # 队列控制 ----------------------------------------------------------------
    def queue_size(self) -> int:
        return len(self.pending)

    def queued_labels(self) -> list[str]:
        return [f"{p['client_id']}#{p['client_op_id']}" for p in self.pending]

    def deliver_one(self, index: int = 0) -> dict[str, Any]:
        """投递队列中的第 index 个请求（默认队首）。"""
        req = self.pending.pop(index)
        try:
            ack = self.service.submit(
                req["doc_id"], req["base_rev"], req["client_id"],
                req["client_op_id"], req["ops"],
            )
            resp = {"ok": True, "rev": ack.rev, "head_rev": ack.head_rev,
                    "ops": [c.to_dict() for c in ack.ops]}
        except OTError as e:
            resp = {"ok": False, "status": e.status,
                    "error": e.to_body()["error"]}
        record = {"request": req, "response": resp}
        self.delivered.append(record)
        return record

    def deliver_all(self, order: list[int] | None = None) -> list[dict[str, Any]]:
        """按 *初始* 下标顺序投递全部请求。

        ``order`` 是调用时刻队列下标的一个排列；每投递一个就从待发
        队列移除。不给定则 FIFO。
        """
        snapshot = list(self.pending)
        order = list(order) if order is not None else list(range(len(snapshot)))
        out = []
        for original_idx in order:
            out.append(self._submit_request(snapshot[original_idx]))
        # 已投递的从待发队列移除
        delivered_ids = {id(r) for r in snapshot}
        self.pending = [p for p in self.pending if id(p) not in delivered_ids]
        return out

    def _submit_request(self, req: dict[str, Any]) -> dict[str, Any]:
        try:
            ack = self.service.submit(
                req["doc_id"], req["base_rev"], req["client_id"],
                req["client_op_id"], req["ops"],
            )
            resp = {"ok": True, "rev": ack.rev, "head_rev": ack.head_rev,
                    "ops": [c.to_dict() for c in ack.ops]}
        except OTError as e:
            resp = {"ok": False, "status": e.status,
                    "error": e.to_body()["error"]}
        record = {"request": req, "response": resp}
        self.delivered.append(record)
        return record

    def deliver_duplicate(self, index: int = 0) -> dict[str, Any]:
        """重复投递同一个请求对象（模拟超时重传/网络重复包）。"""
        import copy

        req = copy.deepcopy(self.pending[index])
        try:
            ack = self.service.submit(
                req["doc_id"], req["base_rev"], req["client_id"],
                req["client_op_id"], req["ops"],
            )
            resp = {"ok": True, "rev": ack.rev, "head_rev": ack.head_rev,
                    "ops": [c.to_dict() for c in ack.ops]}
        except OTError as e:
            resp = {"ok": False, "status": e.status,
                    "error": e.to_body()["error"]}
        return {"request": req, "response": resp}

    # Transport 接口 -----------------------------------------------------------
    def create_document(self, doc_id, initial_text=""):
        v = self.service.create_document(doc_id, initial_text)
        self._doc_id = doc_id
        return {"doc_id": v.doc_id, "rev": v.rev, "text": v.text,
                "baseline_rev": v.baseline_rev}

    def submit(self, doc_id, base_rev, client_id, client_op_id, ops):
        req = {"doc_id": doc_id, "base_rev": base_rev,
               "client_id": client_id, "client_op_id": client_op_id,
               "ops": [dict(c) for c in ops]}
        self.pending.append(req)
        return {"queued": True, "label": f"{client_id}#{client_op_id}"}

    def history(self, doc_id, since):
        revs = self.service.history(doc_id, since=since, limit=10_000)
        return {
            "baseline_rev": self.service.repo.baseline_rev(doc_id),
            "head_rev": self.service.repo.head_rev(doc_id),
            "revisions": [
                {"rev": r.rev, "client_id": r.client_id,
                 "client_op_id": r.client_op_id, "base_rev": r.base_rev,
                 "length_before": r.length_before, "length_after": r.length_after,
                 "checksum": r.checksum, "ops": [c.to_dict() for c in r.ops]}
                for r in revs
            ],
        }

    def catchup(self, doc_id):
        v = self.service.catchup(doc_id)
        return {"rev": v.rev, "baseline_rev": v.baseline_rev, "text": v.text}


class HttpTransport(Transport):
    """通过 httpx 访问运行中的 FastAPI 服务。"""

    def __init__(self, base_url: str, doc_id: str, timeout: float = 10.0):
        import httpx

        self.client = httpx.Client(base_url=base_url, timeout=timeout)
        self.doc_id = doc_id

    def close(self):
        self.client.close()

    def _check(self, resp):
        if resp.status_code >= 400:
            body = resp.json()
            err = body.get("error", {})
            e = OTError(err.get("message", resp.text),
                        reason=err.get("reason"), details=err.get("details", {}))
            e.status = resp.status_code
            e.code = err.get("code", "ERROR")
            raise e
        return resp.json()

    def create_document(self, doc_id, initial_text=""):
        resp = self.client.post("/documents",
                                json={"doc_id": doc_id, "initial_text": initial_text})
        return self._check(resp)

    def submit(self, doc_id, base_rev, client_id, client_op_id, ops):
        resp = self.client.post(
            f"/documents/{doc_id}/submit",
            json={"base_rev": base_rev, "client_id": client_id,
                  "client_op_id": client_op_id, "ops": ops},
        )
        return self._check(resp)

    def history(self, doc_id, since):
        resp = self.client.get(f"/documents/{doc_id}/history",
                               params={"since": since, "limit": 10_000})
        return self._check(resp)

    def catchup(self, doc_id):
        resp = self.client.get(f"/documents/{doc_id}/catchup")
        return self._check(resp)
