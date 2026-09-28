"""Service layer: orchestrates the streaming core, store and resource limits.

Data/error contract with the HTTP layer
---------------------------------------
* All numeric input arrives as 1-D float64 NumPy arrays.
* Every service failure is a :class:`resamp.errors.ResampError` subclass; the
  HTTP layer only maps ``category``/``http_status``, it never invents errors.
* Per-job resampler objects live in an in-memory registry keyed by job id; the
  persisted counters in SQLite are authoritative for audit/replay.
"""
from __future__ import annotations

import base64
import threading

import numpy as np

from .config import Settings
from .dsp.polyphase import PolyphaseResampler
from .errors import InvalidInputError, ResourceExhaustedError, StateConflictError
from .storage import JobStore


def decode_samples(payload: dict, *, list_cap: int) -> np.ndarray:
    """Decode an API payload into a 1-D float64 array.

    Accepted encodings: ``{"encoding":"base64-float64-le","data":"..."}`` or
    ``{"encoding":"json","data":[...]}`` (capped at ``list_cap`` samples).
    """
    enc = payload.get("encoding")
    data = payload.get("data")
    if enc == "base64-float64-le":
        if not isinstance(data, str):
            raise InvalidInputError(
                "base64-float64-le requires a string 'data' field",
                details={"encoding": enc})
        try:
            raw = base64.b64decode(data, validate=True)
        except Exception as exc:
            raise InvalidInputError(
                "invalid base64 payload", details={"reason": str(exc)}) from exc
        if len(raw) % 8:
            raise InvalidInputError(
                "base64 payload length is not a multiple of 8 bytes",
                details={"bytes": len(raw)})
        arr = np.frombuffer(raw, dtype="<f8").astype(np.float64)
    elif enc == "json":
        if not isinstance(data, list):
            raise InvalidInputError(
                "json encoding requires a list 'data' field",
                details={"encoding": enc})
        if len(data) > list_cap:
            raise ResourceExhaustedError(
                f"json list of {len(data)} exceeds cap {list_cap}; "
                "use base64-float64-le",
                details={"samples": len(data), "cap": list_cap})
        try:
            arr = np.asarray(data, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise InvalidInputError(
                "json samples must be numbers",
                details={"reason": str(exc)}) from exc
    else:
        raise InvalidInputError(
            f"unknown encoding {enc!r}; use 'json' or 'base64-float64-le'",
            details={"encoding": enc})
    if arr.ndim != 1:
        raise InvalidInputError("decoded payload is not one-dimensional",
                                details={"ndim": arr.ndim})
    if arr.size and not np.all(np.isfinite(arr)):
        bad = int(np.isfinite(arr).argmin())
        raise InvalidInputError(
            "payload contains non-finite sample",
            details={"index": bad, "value": float(arr[bad])})
    return arr


def encode_samples(arr: np.ndarray) -> dict:
    return {
        "encoding": "base64-float64-le",
        "n_samples": int(arr.size),
        "data": base64.b64encode(
            np.asarray(arr, dtype="<f8").tobytes()).decode("ascii"),
    }


class ResamplingService:
    def __init__(self, settings: Settings, store: JobStore | None = None) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.store = store or JobStore(settings.db_path, settings.data_dir)
        self._engines: dict[str, PolyphaseResampler] = {}
        self._lock = threading.RLock()

    def close(self) -> None:
        self.store.close()

    # -------------------------------------------------------------- lifecycle
    def create_job(self, *, fin: int, fout: int,
                   output_dtype: str = "float64") -> dict:
        engine = PolyphaseResampler(
            fin=fin, fout=fout, output_dtype=output_dtype,
            attenuation_db=self.settings.attenuation_db,
            transition_half_width=self.settings.transition_half_width,
            max_taps=self.settings.max_filter_taps,
            max_rate=self.settings.max_rate,
            max_factor=self.settings.max_ratio_factor,
            max_input_chunk=self.settings.max_input_chunk_samples,
        )
        summary = engine.design_summary()
        with self._lock:
            job = self.store.create_job(
                fin=fin, fout=fout, l=engine.ratio.l, m=engine.ratio.m,
                output_dtype=output_dtype, design_info=summary)
            self._engines[job["job_id"]] = engine
        return {"job": job, "design": summary}

    def _engine(self, job_id: str) -> PolyphaseResampler:
        try:
            return self._engines[job_id]
        except KeyError:
            raise StateConflictError(
                f"job {job_id} is not active in this process "
                "(job state lives in SQLite but its stream was not opened here)",
                details={"job_id": job_id})

    def push(self, job_id: str, x: np.ndarray) -> dict:
        with self._lock:
            job = self.store.require_state(job_id, "created", "running")
            engine = self._engine(job_id)
            if engine.flushed:  # pragma: no cover - guarded by state
                raise StateConflictError("job already flushed",
                                         details={"job_id": job_id})
            if x.size > self.settings.max_input_chunk_samples:
                raise ResourceExhaustedError(
                    f"chunk of {x.size} exceeds limit "
                    f"{self.settings.max_input_chunk_samples}",
                    details={"chunk_samples": x.size,
                             "limit": self.settings.max_input_chunk_samples})
            projected = job["total_in"] + x.size
            if projected > self.settings.max_total_input_samples:
                raise ResourceExhaustedError(
                    f"job total input would be {projected}, exceeding limit "
                    f"{self.settings.max_total_input_samples}",
                    details={"projected": projected,
                             "limit": self.settings.max_total_input_samples})
            try:
                out = engine.push(x)
            except Exception:
                self.store.mark_failed(job_id, "computation_failed",
                                       "chunk processing failed")
                raise
            self.store.append_output(job_id, out)
            self.store.record_chunk(job_id, "push", x.size, out.size)
            job = self.store.get_job(job_id)
            return {"job": job, "n_output": int(out.size),
                    "output": encode_samples(out)}

    def flush(self, job_id: str) -> dict:
        with self._lock:
            self.store.require_state(job_id, "created", "running")
            engine = self._engine(job_id)
            try:
                out = engine.flush()
            except Exception:
                self.store.mark_failed(job_id, "computation_failed",
                                       "flush failed")
                raise
            self.store.append_output(job_id, out)
            self.store.record_chunk(job_id, "flush", 0, out.size)
            self.store.mark_completed(job_id)
            job = self.store.get_job(job_id)
            self._engines.pop(job_id, None)
            return {"job": job, "n_output": int(out.size),
                    "output": encode_samples(out)}

    # ----------------------------------------------------------------- queries
    def status(self, job_id: str) -> dict:
        with self._lock:
            return {"job": self.store.get_job(job_id)}

    def chunks(self, job_id: str) -> dict:
        with self._lock:
            return {"chunks": self.store.get_chunks(job_id)}

    def result(self, job_id: str) -> dict:
        with self._lock:
            job = self.store.get_job(job_id)
            arr = self.store.read_output(job_id)
            return {"job": job, "n_samples": int(arr.size),
                    "output": encode_samples(arr)}

    # ---------------------------------------------------------------- offline
    def run_offline(self, x: np.ndarray, *, fin: int, fout: int,
                    output_dtype: str = "float64",
                    chunk_size: int | None = None) -> tuple[np.ndarray, dict]:
        """Convenience full-file path used by the WAV endpoint."""
        created = self.create_job(fin=fin, fout=fout, output_dtype=output_dtype)
        job_id = created["job"]["job_id"]
        step = chunk_size or self.settings.max_input_chunk_samples
        outputs = []
        for i in range(0, x.size, step):
            outputs.append(self.push(job_id, x[i:i + step])["output"])
        f = self.flush(job_id)
        arr = self.store.read_output(job_id)
        return arr, f["job"]
