"""Streaming job orchestration: ties PCM chunks to the loudness meter.

A job is created with a declared raw format; each ``/jobs/{id}/chunks`` call
appends bytes. Bytes that do not form a complete inter-channel frame are held
back and prepended to the next chunk, so arbitrary chunk boundaries (including
mid-sample) never corrupt the measurement.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .errors import JobError
from .jobstore import STATE_FAILED, STATE_FINALIZED, JobStore
from .loudness import StreamingLoudnessMeter
from .media import decode_pcm
from .serialization import result_to_dict


@dataclass
class _RuntimeJob:
    id: str
    channels: int
    sample_format: str
    roles: list[str]
    meter: StreamingLoudnessMeter
    leftover: bytearray = field(default_factory=bytearray)
    started: float = field(default_factory=time.perf_counter)


class JobManager:
    def __init__(self, store: JobStore, max_bytes: int):
        self._store = store
        self._max_bytes = max_bytes
        self._jobs: dict[str, _RuntimeJob] = {}
        self._lock = threading.Lock()

    def create(self, *, channels: int, sample_format: str, roles: list[str],
               request_id: str | None) -> str:
        job_id = self._store.create(
            channels=channels, sample_format=sample_format, roles=roles,
            request_id=request_id,
        )
        meter = StreamingLoudnessMeter(channels, roles=roles)
        self._jobs[job_id] = _RuntimeJob(
            id=job_id, channels=channels, sample_format=sample_format,
            roles=roles, meter=meter,
        )
        return job_id

    def _get_open(self, job_id: str) -> _RuntimeJob:
        row = self._store.get(job_id)
        if row is None:
            raise JobError(f"unknown job id {job_id!r}",
                           details={"job_id": job_id})
        if row["state"] == STATE_FINALIZED:
            raise JobError("job already finalized; chunks are no longer accepted",
                           details={"job_id": job_id, "state": row["state"]})
        if row["state"] == STATE_FAILED:
            raise JobError(
                f"job previously failed: {row['error_code']}",
                details={"job_id": job_id, "error_code": row["error_code"]},
            )
        job = self._jobs.get(job_id)
        if job is None:  # row OPEN but meter gone (server restart)
            raise JobError(
                "job state is unavailable after a server restart; create a new "
                "job (streaming state is kept in process memory)",
                details={"job_id": job_id},
            )
        return job

    def append(self, job_id: str, data: bytes) -> None:
        with self._lock:
            job = self._get_open(job_id)
            new_total = row_bytes(self._store, job_id) + len(data)
            if new_total > self._max_bytes:
                self._store.fail(job_id, "JOB_ERROR",
                                 f"job byte limit {self._max_bytes} exceeded")
                raise JobError(
                    f"uploaded byte total {new_total} exceeds limit "
                    f"{self._max_bytes}",
                    details={"limit": self._max_bytes, "bytes": new_total},
                )
            buf = bytes(job.leftover) + data
            try:
                samples, leftover = decode_pcm(
                    buf, job.sample_format, job.channels
                )
            except (ValueError, OverflowError) as exc:
                self._store.fail(job_id, "INVALID_MEDIA", str(exc))
                raise JobError(f"PCM decode failed: {exc}") from exc
            if samples.shape[0]:
                job.meter.push(samples)
            job.leftover = bytearray(leftover)
            self._store.add_bytes(job_id, len(data))

    def finalize(self, job_id: str) -> dict:
        with self._lock:
            job = self._get_open(job_id)
            result = job.meter.finalize()
            processing_ms = (time.perf_counter() - job.started) * 1000.0
            payload = result_to_dict(result)
            payload["job_id"] = job_id
            payload["leftover_bytes_held"] = len(job.leftover)
            if job.leftover:
                payload.setdefault("warnings", []).append(
                    f"{len(job.leftover)} bytes did not form a complete frame "
                    "and were never decoded"
                )
            self._store.finalize(job_id, payload, processing_ms)
            self._jobs.pop(job_id, None)
            return payload

    def fail(self, job_id: str, code: str, message: str) -> None:
        with self._lock:
            self._store.fail(job_id, code, message)
            self._jobs.pop(job_id, None)


def row_bytes(store: JobStore, job_id: str) -> int:
    row = store.get(job_id)
    return int(row["bytes_received"]) if row else 0
