"""Service orchestration: kernel + metadata transaction + verification.

Failure handling is deliberately explicit: classified errors roll the
success transaction back and persist a ``failed`` run; genuinely unexpected
exceptions are surfaced as INTERNAL_ERROR (500), never as success.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..adapters.json_out import encode_to_dict
from ..core.decode import verify_roundtrip
from ..core.encode import encode_run
from ..core.errors import DictSvcError
from ..core.model import BatchInput
from ..metadata.store import MetadataStore
from .logging_setup import RunLogger


class Service:
    def __init__(self, store: MetadataStore, run_logger: RunLogger,
                 version_info: dict) -> None:
        self.store = store
        self.run_logger = run_logger
        self.version_info = version_info

    def run(self, batches: list[BatchInput], options: dict) -> dict:
        run_id = options.get("run_id") or self._new_run_id()
        log = self.run_logger.run(run_id, self.version_info)
        events: list[dict] = []

        def event(payload: dict) -> None:
            events.append(payload)
            log.event(payload)

        try:
            for b in batches:
                event({"step": "request_batch", "batch_id": b.batch_id,
                       "rows": len(b.indices),
                       "declared": len(b.values),
                       "null_rows_input": sum(1 for v in b.valid if not v)})
            if self.store.exists(run_id):
                from ..core.errors import RunConflict
                raise RunConflict(f"run_id {run_id!r} already exists")

            enc = encode_run(
                batches,
                target_width=options["target_width"],
                width_policy=options["width_policy"],
                on_duplicate_values=options["on_duplicate_values"],
                event=event)

            originals = {b.batch_id: b for b in batches}
            report = verify_roundtrip(enc, originals)
            event({"step": "roundtrip", "all_match": report.all_match,
                   "checked_rows": report.checked_rows,
                   "mismatches": len(report.mismatches)})
            if not report.all_match:
                # A kernel invariant failure: refuse rather than store an
                # encoding that does not decode back to the inputs.
                raise DictSvcError(
                    "roundtrip verification failed",
                    details={"mismatches": [
                        {"batch_id": m.batch_id, "row": m.row}
                        for m in report.mismatches]})

            # Metadata is the commit point: run + batches + events atomically.
            self.store.save_success(run_id, enc, events)
            log.verdict(True, "roundtrip all rows matched; metadata committed",
                        {"cardinality": enc.cardinality,
                         "global_index_width": enc.global_index_width})
            return encode_to_dict(enc, run_id=run_id, report=report,
                                  events=events)
        except DictSvcError as exc:
            self._fail(log, run_id, events, exc)
            raise
        except Exception as exc:  # noqa: BLE001 - classified at the boundary
            wrapped = DictSvcError(f"unexpected error: {exc}")
            self._fail(log, run_id, events, wrapped)
            raise wrapped from exc
        finally:
            log.close()

    @staticmethod
    def _new_run_id() -> str:
        return "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") \
            + "-" + uuid.uuid4().hex[:12]

    def _fail(self, log, run_id: str, events: list[dict],
              exc: DictSvcError) -> None:
        try:
            self.store.save_failure(run_id, exc.category, exc.message, events)
        except Exception as persist_exc:  # noqa: BLE001
            log.event({"step": "persist_failure_failed",
                       "error": repr(persist_exc)})
        log.verdict(False, f"error category={exc.category}",
                    {"message": exc.message, "details": exc.details})
