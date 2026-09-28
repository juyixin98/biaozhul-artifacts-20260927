"""Service orchestration: parsing -> canonical graph -> (optional) extraction.

This is the only layer that combines the parser, security kernel, isolation
workspace, audit DB and per-run logger. API handlers stay thin; all state
transitions and every decision happen here and are audited.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

from .audit.audit import AuditDB, sha256_bytes
from .config import Settings
from .isolation.workspace import RunWorkspace, WorkspaceManager
from .kernel import canonical
from .kernel.canonical import Plan
from .kernel.errors import ArchiveError, UploadTooLarge
from .kernel.extractor import execute_plan
from .kernel.parser import open_parser
from .logging_setup import RunLogger


@dataclass
class StageFailure(Exception):
    """Carries the failing stage alongside an ArchiveError."""

    stage: str
    error: ArchiveError


@dataclass
class ServiceResult:
    verdict: str  # "accepted" | "extracted" | "rejected" | "error"
    run_id: str
    plan: Plan | None = None
    manifest: dict | None = None
    workspace: RunWorkspace | None = None
    stage: str | None = None
    error: ArchiveError | Exception | None = None


_SAFE_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def safe_upload_name(name: str) -> str:
    name = Path(name).name or "upload.bin"
    cleaned = "".join(c for c in name if c in _SAFE_NAME_CHARS)
    return cleaned[:128] or "upload.bin"


class GuardService:
    def __init__(self, settings: Settings, audit: AuditDB):
        self.settings = settings
        self.audit = audit
        self.workspaces = WorkspaceManager(settings.workspace_root)

    # ------------------------------------------------------------------
    def inspect(self, data: bytes, filename: str) -> ServiceResult:
        """Build and validate the canonical plan; extract nothing."""
        return self._run(data, filename, extract=False)

    def extract(self, data: bytes, filename: str) -> ServiceResult:
        return self._run(data, filename, extract=True)

    # ------------------------------------------------------------------
    def _run(self, data: bytes, filename: str, *, extract: bool) -> ServiceResult:
        ws = self.workspaces.create_run()
        digest = sha256_bytes(data)
        logger = RunLogger(
            ws.run_id,
            self.settings.version,
            ws.log_path,
            self.audit,
            input_sha256=digest,
            filename=filename,
        )
        result = ServiceResult(
            verdict="error", run_id=ws.run_id, workspace=ws
        )
        operation = "extract" if extract else "inspect"
        parser_holder = None
        logger.progress(
            "receive",
            f"{operation} request accepted for processing",
            detail={
                "filename": filename,
                "input_size": len(data),
                "run_root": str(ws.root),
                "version": self.settings.version,
            },
        )
        self.audit.create_run(
            ws.run_id,
            filename=filename,
            container=None,
            input_sha256=digest,
            input_size=len(data),
        )

        try:
            # Stage 1: upload budget (before touching disk).
            if len(data) > self.settings.max_upload_bytes:
                raise StageFailure(
                    "upload",
                    UploadTooLarge(
                        f"upload {len(data)} bytes exceeds limit "
                        f"{self.settings.max_upload_bytes}",
                        evidence=f"size={len(data)}",
                    ),
                )

            # Stage 2: persist the input inside the isolated run directory.
            stored_name = safe_upload_name(filename)
            stored_path = ws.input_dir / stored_name
            stored_path.write_bytes(data)
            logger.progress(
                "persist_input",
                "input bytes stored inside isolated run directory",
                detail={"path": str(stored_path), "sha256": digest},
            )

            # Stage 3: parse evidence. The parser stays open until extraction
            # finishes because payload opener streams reference its archive
            # handle (zipfile/tarfile); it is closed in the finally block.
            try:
                parser_holder = open_parser(data)
                container = parser_holder.container
                evidence_openers = list(parser_holder.entries())
                compressed_bytes = parser_holder.declared_compressed_size()
            except ArchiveError as exc:
                raise StageFailure("parse", exc) from exc
            self.audit.set_container(ws.run_id, container)
            evidence_list = [e for e, _ in evidence_openers]
            openers = [o for _, o in evidence_openers]
            logger.progress(
                "parse",
                f"evidence parsed: container={container} entries={len(evidence_list)}",
                detail={
                    "container": container,
                    "entry_count": len(evidence_list),
                    "stored_payload_bytes": compressed_bytes,
                    "kinds": [e.kind.value for e in evidence_list],
                },
            )

            # Stage 4: canonical target graph + budgets (the security kernel).
            try:
                plan = canonical.build_plan(
                    container,
                    evidence_list,
                    openers,
                    budgets=self.settings.budgets,
                    policy=self.settings.policy,
                    compressed_bytes=compressed_bytes,
                )
            except ArchiveError as exc:
                raise StageFailure("canonical_plan", exc) from exc
            result.plan = plan
            logger.accepted(
                "canonical_plan",
                "canonical target graph accepted; no escape/collision/budget violation",
                detail={
                    "steps": plan.steps,
                    "files": plan.stats.files,
                    "directories": plan.stats.directories,
                    "symlinks": plan.stats.symlinks,
                    "total_declared_bytes": plan.stats.total_declared_bytes,
                    "worst_compression_ratio": plan.stats.worst_compression_ratio,
                },
            )

            if not extract:
                self.audit.finalize_run(
                    ws.run_id,
                    verdict="accepted",
                    category=None,
                    summary={
                        "container": container,
                        "stats": plan.stats.__dict__,
                        "operation": "inspect",
                    },
                )
                logger.accepted(
                    "finalize",
                    "inspection finished: archive is safe to extract under current budget",
                )
                result.verdict = "accepted"
                return result

            # Stage 5: controlled streaming extraction.
            logger.progress(
                "extract_begin",
                f"executing {len(plan.actions)} ordered actions inside output dir",
                detail={"output_dir": str(ws.output_dir)},
            )
            try:
                manifest = execute_plan(plan, ws.output_dir, self.settings.budgets)
            except ArchiveError as exc:
                raise StageFailure("extract_runtime", exc) from exc
            result.manifest = manifest
            logger.accepted(
                "extract_complete",
                f"extracted {manifest['entries_written']} files "
                f"({manifest['total_bytes']} bytes); all outputs confined to run dir",
                detail=manifest,
            )
            self.audit.finalize_run(
                ws.run_id,
                verdict="extracted",
                category=None,
                summary={
                    "container": container,
                    "stats": plan.stats.__dict__,
                    "manifest": {
                        "entries_written": manifest["entries_written"],
                        "total_bytes": manifest["total_bytes"],
                    },
                    "operation": "extract",
                },
            )
            result.verdict = "extracted"
            return result

        except StageFailure as sf:
            return self._reject(ws, logger, result, sf.stage, sf.error, digest, filename)
        except ArchiveError as exc:
            # Defensive: any ArchiveError not wrapped with a precise stage.
            return self._reject(ws, logger, result, "unknown_stage", exc, digest, filename)
        except Exception as exc:  # unexpected — never reported as success
            logger.failed(
                "unexpected",
                f"unhandled {type(exc).__name__}: {exc}",
                detail={"traceback": traceback.format_exc(limit=8)},
            )
            self.audit.finalize_run(
                ws.run_id, verdict="error", category=type(exc).__name__,
                summary={"message": str(exc)},
            )
            result.verdict = "error"
            result.stage = "unexpected"
            result.error = exc
            # Same isolation guarantee as policy rejections: ensure no partial
            # output survives; retain input + log inside the run dir.
            import shutil

            shutil.rmtree(ws.output_dir, ignore_errors=True)
            return result
        finally:
            if parser_holder is not None:
                try:
                    parser_holder.close()
                except Exception:
                    pass
            logger.close()

    def _reject(
        self, ws: RunWorkspace, logger: RunLogger, result: ServiceResult,
        stage: str, exc: ArchiveError, digest: str, filename: str,
    ) -> ServiceResult:
        logger.rejected(
            stage,
            exc.category,
            exc.message,
            evidence=exc.evidence,
        )
        self.audit.finalize_run(
            ws.run_id, verdict="rejected", category=exc.category,
            summary={"stage": stage, "message": exc.message, "evidence": exc.evidence},
        )
        result.verdict = "rejected"
        result.stage = stage
        result.error = exc
        # Isolation guarantee: the extractor has already removed any partial
        # output dir; pre-flight rejections never created one. The run's input
        # bytes and run.log are deliberately retained *inside* the isolated run
        # directory as forensic evidence; nothing is written outside it.
        if ws.output_dir.exists():
            import shutil

            shutil.rmtree(ws.output_dir, ignore_errors=True)
        return result
