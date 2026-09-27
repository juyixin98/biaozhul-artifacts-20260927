"""后台作业执行器：单线程顺序执行，状态写回 SQLite。"""

from __future__ import annotations

import queue
import threading
import traceback
import uuid
from typing import Any, Callable, Optional

from app import __version__
from app.jobs.store import JobStore
from app.logging_setup import get_logger, set_job_id

log = get_logger("app.jobs.runner")


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:16]


class JobRunner:
    def __init__(self, store: JobStore) -> None:
        self.store = store
        self._q: queue.Queue[str] = queue.Queue()
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def register(self, kind: str,
                 handler: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._handlers[kind] = handler

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="job-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put("")  # 唤醒
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def submit(self, kind: str, request_id: str,
               payload: dict[str, Any]) -> str:
        job_id = new_job_id()
        self.store.create(job_id, kind, request_id, payload)
        self._q.put(job_id)
        return job_id

    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._q.get()
            if not job_id:
                continue
            row = self.store.get(job_id)
            if row is None:
                continue
            set_job_id(job_id)
            kind = row["kind"]
            handler = self._handlers.get(kind)
            self.store.set_status(job_id, "running")
            log.info("job_start", extra={"fields": {
                "job_id": job_id, "kind": kind, "version": __version__}})
            try:
                if handler is None:
                    raise RuntimeError(f"无 {kind} 的处理器")
                result = handler(row["payload"])
                result.setdefault("version", __version__)
                result.setdefault("job_id", job_id)
                self.store.save_result(job_id, result)
                log.info("job_succeeded", extra={"fields": {
                    "job_id": job_id, "kind": kind}})
            except Exception as exc:  # noqa: BLE001 - 作业失败要落库而非崩溃线程
                tb = traceback.format_exc(limit=4)
                self.store.set_status(job_id, "failed", error=str(exc))
                log.error("job_failed", extra={"fields": {
                    "job_id": job_id, "kind": kind,
                    "error": str(exc), "trace": tb}})
            finally:
                set_job_id("-")
