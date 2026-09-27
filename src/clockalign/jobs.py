"""Asynchronous job execution and artifact management.

Jobs run on a small thread pool so the API stays responsive. A job persists
each pipeline step to the store and, on success, writes four artifacts:

* ``corrected_slave.wav``  -- the resampled audio (correction artifact)
* ``timeline_map.json``    -- metadata time mapping (kept separate, per rule 3)
* ``report.json``          -- estimate, residuals, usable intervals, warnings
* ``overlay.wav``          -- reference + corrected slave interleaved, for
                              residual/quality spot checks
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import __version__
from .config import Config
from .errors import ClockAlignError
from .logging_setup import bind, get_logger
from .media import load_pair, write_pcm_wav
from .pipeline import run_alignment
from .storage import JobStore

log = get_logger("jobs")


class JobService:
    def __init__(self, cfg: Config, store: JobStore, max_workers: int = 2):
        self.cfg = cfg
        self.store = store
        self._pool = ThreadPoolExecutor(max_workers=max_workers,
                                        thread_name_prefix="align")
        self.artifacts_root = cfg.storage.artifacts_path
        self.artifacts_root.mkdir(parents=True, exist_ok=True)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False)

    def submit(self, job_id: str, request: dict) -> None:
        self._pool.submit(self._run, job_id, request)

    def _run(self, job_id: str, request: dict) -> None:
        bind(request_id=job_id and request.get("request_id"), job_id=job_id)
        self.store.set_status(job_id, "running")
        self.store.add_event(job_id, "job.started", "ok",
                             {"version": __version__,
                              "config_file": str(self.cfg.source)})
        try:
            reference, slave = load_pair(
                request.get("reference_path"), request.get("slave_path"),
                stereo_path=request.get("stereo_path"),
                stereo_role=self.cfg.media.stereo_channel_role,
                max_sample_rate=self.cfg.media.max_sample_rate,
                sample_rate_mismatch_ppm_max=(
                    self.cfg.media.sample_rate_mismatch_ppm_max))
            result = run_alignment(
                reference, slave, self.cfg, mode=request.get("mode"),
                external_time_anchor=bool(request.get("external_time_anchor",
                                                      False)))
            for step in result.steps:
                self.store.add_event(job_id, step.name, step.status, step.detail)

            job_dir = self.artifacts_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            payload = result.to_dict()

            if result.status == "corrected" and result.correction is not None:
                wav_path = job_dir / "corrected_slave.wav"
                write_pcm_wav(wav_path, result.correction.corrected_audio,
                              result.correction.sample_rate)
                overlay = _overlay(reference.samples,
                                   result.correction.corrected_audio)
                write_pcm_wav(job_dir / "overlay.wav", overlay,
                              result.correction.sample_rate)
                with open(job_dir / "timeline_map.json", "w",
                          encoding="utf-8") as fh:
                    json.dump(result.timeline_map, fh, indent=2, sort_keys=True)
                payload["artifacts"] = {
                    "corrected_audio": str(wav_path),
                    "timeline_map": str(job_dir / "timeline_map.json"),
                    "overlay": str(job_dir / "overlay.wav"),
                }
                status = "succeeded"
            else:
                status = "rejected_insufficient_evidence"

            report_path = job_dir / "report.json"
            with open(report_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
            self.store.set_status(job_id, status,
                                  failure=result.failure,
                                  result_path=str(report_path))
            self.store.add_event(job_id, "job.finished", "ok",
                                 {"status": status})
        except ClockAlignError as exc:
            failure = {"code": exc.code, "message": exc.message,
                       "details": exc.details}
            self.store.set_status(job_id, "failed", failure=failure)
            self.store.add_event(job_id, "job.failed", "error", failure)
            log.warning("job failed: %s", exc.message)
        except Exception as exc:  # pragma: no cover - defensive boundary
            failure = {"code": "internal_error", "message": repr(exc)}
            self.store.set_status(job_id, "failed", failure=failure)
            self.store.add_event(job_id, "job.failed", "error", failure)
            log.exception("unhandled job error")


def _overlay(reference, corrected):
    n = min(len(reference), len(corrected))
    stereo = np.zeros((n, 2), dtype=np.float32)
    stereo[:, 0] = reference[:n]
    stereo[:, 1] = corrected[:n]
    return stereo
