"""Job lifecycle management: queued analysis runs on a small worker pool."""
from __future__ import annotations

import datetime
import hashlib
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from ..config import Settings
from ..demux import StreamAnalyzer
from .store import JobStore, STATUS_DONE, STATUS_FAILED, STATUS_RUNNING


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def analyze_bytes(
    data: bytes,
    settings: Settings,
    record_id: str,
):
    """Pure synchronous analysis; used by workers and by /validate.

    Returns ``(report_dict, AnalysisResult)``. The report caps embedded
    events for size; the result object retains every diagnostic so the
    job store can persist them all.
    """
    analyzer = StreamAnalyzer(settings, record_id=record_id)
    result = analyzer.analyze(data)
    report = result.report_dict(event_limit=settings.report_event_limit)
    report["record_id"] = record_id
    return report, result


class JobManager:
    def __init__(self, store: JobStore, settings: Settings):
        self._store = store
        self._settings = settings
        self._pool = ThreadPoolExecutor(
            max_workers=settings.job_workers, thread_name_prefix="ts-job"
        )
        self._done_events: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()

    def submit(self, data: bytes, input_name: Optional[str]) -> str:
        job_id = uuid.uuid4().hex
        sha = hashlib.sha256(data).hexdigest()
        self._store.create_job(job_id, _now(), input_name, len(data), sha)
        with self._events_lock:
            self._done_events[job_id] = threading.Event()
        self._store.purge_oldest(self._settings.max_jobs)
        self._pool.submit(self._run, job_id, data, input_name, sha)
        return job_id

    def _run(
        self, job_id: str, data: bytes, input_name: str, sha: str
    ) -> None:
        self._store.set_status(job_id, STATUS_RUNNING, _now())
        try:
            report, result = analyze_bytes(data, self._settings, record_id=job_id)
            report["input"] = {
                "name": input_name,
                "size": len(data),
                "sha256": sha,
            }
            # Full diagnostics land in the DB even when the report caps them.
            self._store.save_events(job_id, result.diagnostics.events)
            self._store.save_report(job_id, report)
            self._store.set_status(job_id, STATUS_DONE, _now())
        except Exception as exc:  # noqa: BLE001 - job boundary, record failure
            self._store.set_status(
                job_id,
                STATUS_FAILED,
                _now(),
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
        finally:
            with self._events_lock:
                event = self._done_events.get(job_id)
            if event is not None:
                event.set()

    def wait(self, job_id: str, timeout: float = 10.0) -> bool:
        """Block until a job finishes. Returns True if it finished."""
        with self._events_lock:
            event = self._done_events.get(job_id)
        if event is None:
            return False
        return event.wait(timeout=timeout)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)
