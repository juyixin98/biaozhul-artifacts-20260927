"""End-to-end inspection orchestration.

Pipeline (each phase emits audit events):

    spool -> detect -> scan/budget -> path graph -> symlink graph
          -> extract -> independent verification -> signed manifest

Any classified rejection aborts before writes (scan/plan) or rolls the run
directory back (extract/verify).  Rejections are returned as a structured
verdict rather than raised; truly unexpected errors are surfaced as
``INTERNAL_ERROR`` and never reported as success.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .archiveio import EntryKind, open_reader
from .audit import AuditLogger
from .budget import Budget
from .config import Config
from .errors import RejectionCategory, RejectionError
from .isolation import (
    create_run_dir,
    new_run_id,
    remove_run_dir,
    remove_spool,
    spool_upload,
)
from .kernel import extract, verify_tree
from .paths import Planner
from .store import Store

log = logging.getLogger("archguard")


@dataclass
class Verdict:
    accepted: bool
    run_id: str
    status: str  # accepted | rejected | error
    category: str | None = None
    detail: str | None = None
    entry: str | None = None
    format: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    verify: dict[str, Any] = field(default_factory=dict)
    manifest_path: str | None = None
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "run_id": self.run_id,
            "status": self.status,
            "failure": None
            if self.accepted
            else {"category": self.category, "detail": self.detail, "entry": self.entry},
            "format": self.format,
            "usage": self.usage,
            "budgets": self.budgets,
            "files": self.files,
            "verification": self.verify,
            "manifest_path": self.manifest_path,
            "steps": self.steps,
            "version": __version__,
        }


class Engine:
    def __init__(self, config: Config, store: Store, audit: AuditLogger) -> None:
        self.config = config
        self.budget = Budget.from_config(config.budget_limits())
        self.budget.validate()
        self.store = store
        self.audit = audit

    def inspect(
        self,
        data: bytes,
        *,
        input_name: str = "upload.bin",
        run_id: str | None = None,
    ) -> Verdict:
        run_id = run_id or new_run_id()
        audit = self.audit
        steps: list[str] = []

        digest = hashlib.sha256(data).hexdigest()
        spool_path = spool_upload(self.config.home, data, run_id)
        audit.event(
            run_id,
            "receive",
            "run_start",
            detail={
                "input_name": input_name,
                "input_size": len(data),
                "input_sha256": digest,
                "spool": str(spool_path),
                "service_version": __version__,
                "budgets": self.budget.to_dict(),
            },
        )
        self.store.create_run(
            run_id,
            input_name=input_name,
            input_sha256=digest,
            input_size=len(data),
        )
        steps.append("receive")

        verdict = Verdict(
            accepted=False,
            run_id=run_id,
            status="error",
            budgets=self.budget.to_dict(),
        )
        out_dir: Path | None = None

        try:
            if len(data) > self.config.max_upload_bytes:
                raise RejectionError(
                    RejectionCategory.UPLOAD_LIMIT,
                    f"upload {len(data)} bytes exceeds limit "
                    f"{self.config.max_upload_bytes}",
                )

            # ---------------------------------------------------------- scan
            with open_reader(data) as reader:
                entries = reader.entries()
                fmt = reader.__class__.__name__.replace("Reader", "").lower()
                steps.append("detect+scan")
                audit.event(
                    run_id,
                    "scan",
                    "entries_read",
                    detail={"format": fmt, "entry_count": len(entries)},
                )

                planner = Planner(self.budget)
                declared_total = 0
                file_entries = 0
                compressed_total = 0

                # First pass: aggregate declared budgets before graph work.
                for entry in entries:
                    if entry.kind is EntryKind.FILE:
                        file_entries += 1
                        declared_total += entry.size
                        compressed_total += entry.compressed_size
                self.budget.check_files(file_entries)
                self.budget.check_total(declared_total)
                self.budget.check_ratio(len(data), declared_total)
                audit.event(
                    run_id,
                    "scan",
                    "budgets_checked",
                    detail={
                        "files": file_entries,
                        "declared_total": declared_total,
                        "archive_size": len(data),
                    },
                )
                steps.append("budget")

                # Second pass: build the canonical path graph.  Symlink target
                # bytes are read here, while the source payload is untouched.
                for entry in entries:
                    link_text = None
                    if entry.kind is EntryKind.SYMLINK:
                        link_text = reader.read_symlink_target(entry)
                    planner.add(entry, link_text)
                planner.resolve_symlinks()
                steps.append("path_graph")
                audit.event(
                    run_id,
                    "plan",
                    "graph_accepted",
                    detail={
                        "nodes": len(planner.plan.nodes),
                        "explicit": len(planner.plan.explicit),
                        "symlinks": len(planner.plan.symlinks()),
                        "max_depth": planner.plan.usage.max_depth,
                    },
                )

                # ----------------------------------------------------- extract
                # create_run_dir returns the materialized output root.
                out_dir = create_run_dir(self.config.home / "runs", run_id)
                audit.event(
                    run_id,
                    "extract",
                    "run_dir_created",
                    detail={"out_dir": str(out_dir)},
                )
                extracted = extract(reader, planner.plan, out_dir, audit, run_id)
                steps.append("extract")

            # Reader closed before verification: checksum bookkeeping is done.
            verify_summary = verify_tree(
                planner.plan, out_dir, extracted, audit, run_id
            )
            steps.append("verify")

            verdict.accepted = True
            verdict.status = "accepted"
            verdict.format = fmt
            verdict.usage = planner.plan.usage.to_dict()
            verdict.verify = verify_summary
            verdict.files = [
                {
                    "declared_path": f.declared_path,
                    "physical_path": f.physical_path,
                    "size": f.size,
                    "sha256": f.sha256,
                    "kind": f.kind,
                }
                for f in extracted
            ]

            manifest = audit.write_manifest(
                run_id,
                status="accepted",
                input_name=input_name,
                input_sha256=digest,
                input_size=len(data),
                fmt=fmt,
                verdict={"accepted": True},
                plan_summary={
                    "nodes": len(planner.plan.nodes),
                    "explicit": len(planner.plan.explicit),
                },
                files=verdict.files,
                failure=None,
                budgets=self.budget.to_dict(),
            )
            verdict.manifest_path = str(manifest)
            audit.event(
                run_id,
                "complete",
                "accepted",
                detail={"files": len(extracted), "manifest": str(manifest)},
            )
            # Input was fully verified; drop its bytes (manifest records hash).
            remove_spool(spool_path)
            self.store.finish_run(
                run_id,
                status="accepted",
                fmt=fmt,
                category=None,
                detail=None,
                entry=None,
                file_count=planner.plan.usage.file_count,
                total_bytes=planner.plan.usage.total_bytes,
                manifest_path=str(manifest),
            )
            return verdict

        except RejectionError as exc:
            steps.append("rejected")
            remove_spool(spool_path)
            return self._reject(
                verdict,
                exc,
                run_id,
                input_name,
                digest,
                len(data),
                out_dir,
                steps,
            )
        except Exception as exc:  # noqa: BLE001 - never hide unknowns as success
            log.exception("unexpected engine error for run %s", run_id)
            audit.event(
                run_id,
                "complete",
                "internal_error",
                detail={"error": repr(exc)},
                level=logging.ERROR,
            )
            if out_dir is not None and not self.config.keep_failed_dirs:
                remove_run_dir(out_dir.parent)
            remove_spool(spool_path)
            verdict.status = "error"
            verdict.category = RejectionCategory.INTERNAL_ERROR.value
            verdict.detail = f"{type(exc).__name__}: {exc}"
            self.store.finish_run(
                run_id,
                status="error",
                fmt=None,
                category=verdict.category,
                detail=verdict.detail,
                entry=None,
                file_count=None,
                total_bytes=None,
                manifest_path=None,
            )
            return verdict

    def _reject(
        self,
        verdict: Verdict,
        exc: RejectionError,
        run_id: str,
        input_name: str,
        digest: str,
        size: int,
        out_dir: Path | None,
        steps: list[str],
    ) -> Verdict:
        audit = self.audit
        audit.event(
            run_id,
            "complete",
            "rejected",
            detail={
                "category": exc.category.value,
                "detail": exc.detail,
                "entry": exc.entry,
            },
            level=logging.WARNING,
        )
        if out_dir is not None and not self.config.keep_failed_dirs:
            remove_run_dir(out_dir.parent)
        manifest = audit.write_manifest(
            run_id,
            status="rejected",
            input_name=input_name,
            input_sha256=digest,
            input_size=size,
            fmt=verdict.format,
            verdict={"accepted": False, "category": exc.category.value},
            plan_summary=None,
            files=None,
            failure=exc.to_dict(),
            budgets=self.budget.to_dict(),
        )
        verdict.accepted = False
        verdict.status = "rejected"
        verdict.category = exc.category.value
        verdict.detail = exc.detail
        verdict.entry = exc.entry
        verdict.manifest_path = str(manifest)
        verdict.steps = steps
        self.store.finish_run(
            run_id,
            status="rejected",
            fmt=verdict.format,
            category=exc.category.value,
            detail=exc.detail,
            entry=exc.entry,
            file_count=None,
            total_bytes=None,
            manifest_path=str(manifest),
        )
        return verdict
