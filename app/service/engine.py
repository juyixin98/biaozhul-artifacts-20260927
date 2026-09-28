"""Orchestration engine: parsing -> kernel -> independent verification ->
SQLite metadata transaction.

The engine owns the RUNNING -> SUCCEEDED/FAILED transaction and emits progress
log lines per computation step. A kernel error marks the job FAILED with its
classified code; an unexpected exception is marked ``INTERNAL_ERROR`` (never
reported as success) and re-raised.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..adapters.jsonio import batch_from_dict, result_to_dict
from ..adapters import arrowio
from ..config import Settings
from ..core.errors import DicunifyError, VerificationMismatchError
from ..core.kernel import BatchInput, UnifyResult, unify
from ..core.verify import verify_roundtrip
from .identity import component_versions, new_run_id
from .logging_setup import get_logger
from ..store.sqlite_store import JobStore


@dataclass(frozen=True)
class EngineResponse:
    job_id: str
    result: UnifyResult
    body: dict


class UnifyEngine:
    def __init__(self, settings: Settings, store: JobStore):
        self.settings = settings
        self.store = store
        self.log = get_logger()

    # ------------------------------------------------------------------- JSON

    def run_json(self, payload: dict) -> EngineResponse:
        run_id = payload.get("client_run_id") or new_run_id()
        job_id = new_run_id()
        versions = component_versions()
        value_type = payload.get("value_type")
        index_policy = payload.get("index_policy", "auto")
        target_width = payload.get("target_width")
        dedupe = bool(payload.get("dedupe_local_dictionary", False))
        raw_batches = payload.get("batches")

        self.store.insert_running(
            job_id=job_id, value_type=value_type or "<unset>",
            index_policy=index_policy or "<unset>",
            target_width=target_width, sort_policy="<pending-validation>",
            versions={**versions, "run_id": run_id},
        )
        try:
            self.log.info("job accepted", extra={
                "run_id": run_id, "job_id": job_id, "step": "accept",
                "progress": {"raw_batches": len(raw_batches) if isinstance(raw_batches, list) else None},
                "versions": versions,
            })

            if not isinstance(raw_batches, list):
                from ..core.errors import MalformedBatchError
                raise MalformedBatchError(
                    "payload.batches must be a list",
                    details={"actual_type": type(raw_batches).__name__},
                )

            batches: list[BatchInput] = []
            normalization: list[dict] = []
            for i, raw in enumerate(raw_batches):
                batch, report = batch_from_dict(
                    raw, value_type=value_type, dedupe=dedupe
                )
                batches.append(batch)
                normalization.append(report)
                self.log.info("batch parsed", extra={
                    "run_id": run_id, "job_id": job_id,
                    "batch_id": batch.batch_id, "step": "parse",
                    "progress": {"ordinal": i, "total": len(raw_batches),
                                 "rows": len(batch.indices),
                                 "local_dict": len(batch.dictionary),
                                 "duplicates_removed": report["duplicate_dictionary_entries"]},
                })

            result = self._run_kernel(
                batches, value_type=value_type, index_policy=index_policy,
                target_width=target_width, job_id=job_id, run_id=run_id,
            )

            # Independent round-trip check (oracle that shares no merge logic).
            decoded = verify_roundtrip(result, batches)
            self.log.info("roundtrip verified", extra={
                "run_id": run_id, "job_id": job_id, "step": "verify",
                "progress": {"batches_checked": len(decoded),
                             "rows_checked": sum(len(d.rows) for d in decoded)},
            })

            self.store.mark_succeeded(job_id=job_id, result=result,
                                     normalization=normalization)
            self.log.info("job succeeded", extra={
                "run_id": run_id, "job_id": job_id, "step": "commit",
                "progress": {"cardinality": result.cardinality,
                             "width": result.index_width_bits},
            })

            body = result_to_dict(result, normalization_reports=normalization,
                                  job_id=job_id)
            body["run_id"] = run_id
            body["versions"] = versions
            return EngineResponse(job_id=job_id, result=result, body=body)

        except DicunifyError as exc:
            self._fail(job_id, run_id, exc.code, exc.message, exc.details)
            raise
        except Exception as exc:  # noqa: BLE001 - classified at the boundary
            self._fail(job_id, run_id, "INTERNAL_ERROR", str(exc),
                       {"type": type(exc).__name__})
            raise

    # ------------------------------------------------------------------ Arrow

    def run_arrow(self, buf: bytes, *, value_type: str,
                  index_policy: str = "auto", target_width: int | None = None,
                  client_run_id: str | None = None) -> tuple[bytes, str]:
        run_id = client_run_id or new_run_id()
        job_id = new_run_id()
        versions = component_versions()
        self.store.insert_running(
            job_id=job_id, value_type=value_type, index_policy=index_policy,
            target_width=target_width, sort_policy="<pending-validation>",
            versions={**versions, "run_id": run_id},
        )
        try:
            batches = arrowio.decode_ipc_batches(buf, value_type=value_type)
            result = self._run_kernel(
                batches, value_type=value_type, index_policy=index_policy,
                target_width=target_width, job_id=job_id, run_id=run_id,
            )
            decoded = verify_roundtrip(result, batches)
            self.log.info("arrow roundtrip verified", extra={
                "run_id": run_id, "job_id": job_id, "step": "verify",
                "progress": {"rows_checked": sum(len(d.rows) for d in decoded)},
            })
            self.store.mark_succeeded(
                job_id=job_id, result=result,
                normalization=[{"batch_id": b.batch_id,
                                "duplicate_dictionary_entries": 0}
                               for b in batches],
            )
            return arrowio.result_to_ipc(result), job_id
        except DicunifyError as exc:
            self._fail(job_id, run_id, exc.code, exc.message, exc.details)
            raise
        except Exception as exc:  # noqa: BLE001
            self._fail(job_id, run_id, "INTERNAL_ERROR", str(exc),
                       {"type": type(exc).__name__})
            raise

    # ------------------------------------------------------------- shared core

    def _run_kernel(self, batches, *, value_type, index_policy, target_width,
                    job_id, run_id) -> UnifyResult:
        self.log.info("kernel start", extra={
            "run_id": run_id, "job_id": job_id, "step": "unify",
            "progress": {"batches": len(batches)},
        })
        result = unify(
            batches,
            value_type=value_type,
            index_policy=index_policy,
            target_width=target_width,
            max_cardinality=self.settings.max_cardinality,
        )
        self.log.info("kernel done", extra={
            "run_id": run_id, "job_id": job_id, "step": "unify",
            "progress": {"cardinality": result.cardinality,
                         "width": result.index_width_bits,
                         "batches": len(result.batch_remaps)},
        })
        return result

    def _fail(self, job_id: str, run_id: str, code: str, message: str,
              details: dict) -> None:
        self.log.error("job failed", extra={
            "run_id": run_id, "job_id": job_id, "step": "fail",
            "details": {"code": code, "message": message, **details},
        })
        try:
            self.store.mark_failed(job_id=job_id, code=code, message=message,
                                   details=details)
        except Exception:  # noqa: BLE001 - failure bookkeeping must not mask
            self.log.exception("could not persist FAILED status",
                               extra={"run_id": run_id, "job_id": job_id})

    # --------------------------------------------------------------- verify API

    def verify_job_decoding(self, job_id: str, provided: dict) -> dict:
        """Stateless independent verification of a supplied result payload.

        Persisted dictionaries are not stored here (only metadata), so clients
        POST the global dictionary + remaps back; the same oracle used at write
        time decodes and compares row by row.
        """
        from ..core.kernel import BatchInput, UnifyResult, BatchRemap

        remaps = []
        originals = []
        for b in provided.get("batches", []):
            remaps.append(BatchRemap(
                batch_id=b["batch_id"],
                local_to_global=tuple(b["local_to_global"]),
                global_indices=tuple(b["global_indices"]),
                validity=tuple(b["validity"]),
                row_count=len(b["global_indices"]),
                null_count=sum(1 for v in b["validity"] if not v),
            ))
            originals.append(BatchInput(
                batch_id=b["batch_id"],
                dictionary=list(b["original_dictionary"]),
                indices=list(b["original_indices"]),
                validity=list(b["validity"]),
            ))
        result = UnifyResult(
            global_dictionary=tuple(provided["global_dictionary"]),
            global_value_type=provided["global_value_type"],
            index_width_bits=provided["index_width_bits"],
            cardinality=len(provided["global_dictionary"]),
            sort_policy=provided.get("sort_policy", "unknown"),
            batch_remaps=tuple(remaps),
            stats={},
        )
        decoded = verify_roundtrip(result, originals)
        return {
            "job_id": job_id,
            "ok": True,
            "rows_checked": sum(len(d.rows) for d in decoded),
            "batches_checked": len(decoded),
        }
