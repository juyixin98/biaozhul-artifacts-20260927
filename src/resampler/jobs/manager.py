"""Job orchestration: ties the kernel, media codecs, limits and store together.

Data/error contract across modules
-----------------------------------
* ratio/filter raise InputValidationError (bad parameters) or
  ResourceExhaustedError (filter tap cap).
* media decoding raises InputValidationError for malformed/non-finite input.
* engine.push may raise ComputationError (non-finite arithmetic).
* output integer encoding applies the job clip policy; "reject" surfaces
  OutputOverflowError and fails the job.

All failures move the job to ``failed`` with category/code/message
persisted for later replay; state violations (chunk after flush, double
flush) are 409 conflicts and *also* persist as failed jobs.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..config import Settings
from ..errors import (InputValidationError, ResourceExhaustedError,
                      ResamplerError, StateConflictError)
from ..media.pcm import PCM_FORMATS, decode_pcm, sample_width
from ..media.wav import parse_wav
from ..observability import RunLogger
from ..signal import StreamingPolyphase, build_plan
from .states import FAILED, FLUSHED, OPEN
from .storage import JobRow, JobStore

CONTAINERS = {"raw", "wav"}


@dataclass
class JobParams:
    input_rate: int
    output_rate: int
    input_format: str = "f64le"
    input_container: str = "raw"
    output_format: str = "f64le"
    output_container: str = "raw"
    clip_policy: str = "clip"
    atten_db: float | None = None
    passband_edge: float | None = None


class JobManager:
    def __init__(self, settings: Settings, store: JobStore, logger: RunLogger):
        self.settings = settings
        self.store = store
        self.log = logger
        self._lock = threading.RLock()
        # job_id -> live kernel + counters (reconstructable from chunks table)
        self._engines: dict[str, StreamingPolyphase] = {}

    # ---------------------------------------------------------------- create
    def create_job(self, params: JobParams, job_id: str | None = None,
                   logger: RunLogger | None = None) -> JobRow:
        log = logger or self.log
        for name, fmt in (("input_format", params.input_format),
                          ("output_format", params.output_format)):
            if fmt not in PCM_FORMATS:
                raise InputValidationError(f"unknown {name}", {"format": fmt})
        for name, c in (("input_container", params.input_container),
                        ("output_container", params.output_container)):
            if c not in CONTAINERS:
                raise InputValidationError(f"unknown {name}",
                                           {"container": c, "known": sorted(CONTAINERS)})
        if params.clip_policy not in {"clip", "reject"}:
            raise InputValidationError("unknown clip_policy",
                                       {"policy": params.clip_policy})
        if params.input_container == "wav":
            # The WAV header carries its own rate; plan is built when the first
            # (and only) chunk arrives. Still validate the requested rate now
            # for raw parity; it must agree with the header later.
            pass

        with self._lock:
            if self.store.count_jobs() >= self.settings.max_jobs:
                raise ResourceExhaustedError(
                    "job table full", {"cap": self.settings.max_jobs})

            job_id = job_id or uuid.uuid4().hex[:16]
            if self.store.get_job(job_id) is not None:
                raise InputValidationError("duplicate job_id", {"job_id": job_id})

            plan = build_plan(params.input_rate, params.output_rate,
                              atten_db=params.atten_db,
                              passband_edge=params.passband_edge,
                              settings=self.settings)
            now = time.time()
            row = JobRow(
                job_id=job_id, state=OPEN,
                input_rate=int(plan.ratio.rate_in), output_rate=int(plan.ratio.rate_out),
                up=plan.up, down=plan.down,
                input_format=params.input_format,
                input_container=params.input_container,
                output_format=params.output_format,
                output_container=params.output_container,
                clip_policy=params.clip_policy,
                atten_db=plan.atten_db, passband_edge=plan.passband_edge_frac,
                taps_per_phase=plan.taps_per_phase, num_taps=plan.num_taps,
                delay_input=plan.delay_input, delay_output=plan.delay_output,
                passband_edge_hz=plan.passband_edge_hz,
                stopband_edge_hz=plan.stopband_edge_hz, cutoff_hz=plan.cutoff_hz,
                input_samples=0, output_samples=0, clipped_samples=0,
                chunks_received=0, error_category=None, error_code=None,
                error_message=None, created_at=now, updated_at=now)
            self.store.create_job(row)
            self._engines[job_id] = StreamingPolyphase(plan)
            log.event("job_created", {"job_id": job_id, "plan": plan.describe()})
            return row

    # ---------------------------------------------------------------- helpers
    def _require_open(self, row: JobRow) -> None:
        if row.state in (FLUSHED, FAILED):
            raise StateConflictError(
                f"job is {row.state}; no more chunks accepted",
                {"job_id": row.job_id, "state": row.state})

    def _fail(self, job_id: str, err: ResamplerError,
              logger: RunLogger | None = None) -> None:
        log = logger or self.log
        self.store.set_state(job_id, FAILED, time.time(),
                             error=(err.category, err.code, err.message))
        log.event("job_failed",
                  {"job_id": job_id, "category": err.category,
                   "code": err.code, "message": err.message, "detail": err.detail})

    # ----------------------------------------------------------------- chunks
    def add_chunk(self, job_id: str, data: bytes,
                  logger: RunLogger | None = None) -> dict[str, Any]:
        log = logger or self.log
        with self._lock:
            row = self.store.get_job(job_id)
            if row is None:
                raise InputValidationError("unknown job_id", {"job_id": job_id})
            self._require_open(row)
            engine = self._engines[job_id]

            try:
                if row.input_container == "wav":
                    if row.chunks_received > 0:
                        raise InputValidationError(
                            "WAV jobs accept exactly one chunk containing the "
                            "complete file", {"chunk_index": row.chunks_received})
                    decoded = parse_wav(bytes(data))
                    if decoded.info.sample_rate != row.input_rate:
                        raise InputValidationError(
                            "WAV sample rate does not match job",
                            {"wav_rate": decoded.info.sample_rate,
                             "job_rate": row.input_rate})
                    samples = decoded.samples
                    fmt = decoded.info.pcm_format
                else:
                    fmt = row.input_format
                    samples = decode_pcm(bytes(data), fmt)

                if samples.size > self.settings.max_samples_per_chunk:
                    raise ResourceExhaustedError(
                        "chunk exceeds sample cap",
                        {"samples": samples.size,
                         "cap": self.settings.max_samples_per_chunk})
                if row.input_samples + samples.size > self.settings.max_total_samples:
                    raise ResourceExhaustedError(
                        "job total-input sample cap exceeded",
                        {"after": row.input_samples + samples.size,
                         "cap": self.settings.max_total_samples})

                # Finite check at the boundary: decode_pcm already rejects
                # non-finite integer/float payloads, but guard explicitly.
                if samples.size and not np.all(np.isfinite(samples)):
                    from ..errors import ComputationError
                    raise ComputationError(
                        "non-finite sample reached the kernel",
                        {"bad": int(np.sum(~np.isfinite(samples)))})

                log.state(job_id, engine.state_snapshot())
                out = engine.push(samples)

                clipped = 0
                if row.output_format in {"u8", "s16le", "s24le", "s32le"}:
                    from ..media.pcm import encode_pcm
                    enc = encode_pcm(out, row.output_format,
                                     clip_policy=row.clip_policy)
                    clipped = enc.clipped

                idx = row.chunks_received
                now = time.time()
                self.store.add_chunk(job_id, idx, bytes(data), samples.size,
                                     row.input_container, now)
                if out.size:
                    self.store.add_output(job_id, idx, out, clipped)
                self.store.update_job_counters(
                    job_id, row.input_samples + samples.size,
                    row.output_samples + out.size,
                    row.clipped_samples + clipped, idx + 1, now)
                log.event("chunk_accepted",
                               {"job_id": job_id, "chunk_index": idx,
                                "input_samples": int(samples.size),
                                "output_samples": int(out.size),
                                "clipped": clipped,
                                "format": fmt,
                                "state": engine.state_snapshot()})
                return {"chunk_index": idx, "input_samples": int(samples.size),
                        "output_samples_emitted": int(out.size),
                        "clipped_samples": clipped,
                        "total_input_samples": row.input_samples + samples.size,
                        "total_output_samples": row.output_samples + out.size,
                        "state": OPEN}
            except ResamplerError as err:
                err.detail.setdefault("job_id", job_id)
                self._fail(job_id, err, logger=log)
                raise

    # ------------------------------------------------------------------ flush
    def flush(self, job_id: str, logger: RunLogger | None = None) -> dict[str, Any]:
        log = logger or self.log
        with self._lock:
            row = self.store.get_job(job_id)
            if row is None:
                raise InputValidationError("unknown job_id", {"job_id": job_id})
            if row.state == FLUSHED:
                raise StateConflictError("job already flushed",
                                         {"job_id": job_id, "state": FLUSHED})
            if row.state == FAILED:
                raise StateConflictError("job previously failed",
                                         {"job_id": job_id, "state": FAILED})
            engine = self._engines[job_id]
            try:
                tail = engine.flush()
                clipped = 0
                if tail.size and row.output_format in {"u8", "s16le", "s24le", "s32le"}:
                    from ..media.pcm import encode_pcm
                    enc = encode_pcm(tail, row.output_format,
                                     clip_policy=row.clip_policy)
                    clipped = enc.clipped
                now = time.time()
                idx = row.chunks_received
                if tail.size:
                    self.store.add_output(job_id, idx, tail, clipped)
                self.store.update_job_counters(
                    job_id, row.input_samples,
                    row.output_samples + tail.size,
                    row.clipped_samples + clipped, row.chunks_received, now)
                self.store.set_state(job_id, FLUSHED, now)
                log.event("job_flushed",
                               {"job_id": job_id, "tail_samples": int(tail.size),
                                "total_outputs": row.output_samples + tail.size,
                                "expected": engine.plan.expected_outputs(row.input_samples),
                                "state": engine.state_snapshot()})
                return {"state": FLUSHED,
                        "tail_samples": int(tail.size),
                        "total_input_samples": row.input_samples,
                        "total_output_samples": int(row.output_samples + tail.size),
                        "clipped_samples": int(row.clipped_samples + clipped)}
            except ResamplerError as err:
                err.detail.setdefault("job_id", job_id)
                self._fail(job_id, err, logger=log)
                raise

    # ----------------------------------------------------------------- result
    def get_result(self, job_id: str):
        with self._lock:
            row = self.store.get_job(job_id)
            if row is None:
                raise InputValidationError("unknown job_id", {"job_id": job_id})
            if row.state != FLUSHED:
                raise StateConflictError(
                    "result available only after flush",
                    {"job_id": job_id, "state": row.state})
            pieces = []
            total_clipped = 0
            for _idx, blob, clipped in self.store.get_outputs(job_id):
                pieces.append(np.frombuffer(blob, dtype="<f8"))
                total_clipped += clipped
            f64 = np.concatenate(pieces) if pieces else np.empty(0, dtype=np.float64)
            return row, np.ascontiguousarray(f64), int(total_clipped)
