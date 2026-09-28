"""Per-run state isolation.

Layout under the service home (everything the service ever writes)::

    $HOME/
      spool/<run_id>.bin          uploaded archive bytes
      runs/<run_id>/out/          extraction root (the only materialized output)
      audit/events.jsonl          hash-chained audit trail
      audit/key.bin               HMAC key (local synthetic secret)
      runs.db                     SQLite state
      manifests/<run_id>.json     signed manifest

Nothing is created outside ``home``.  Rejected runs leave only their spool
file (removed at the end of the request) plus audit/database rows.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from pathlib import Path

from .errors import RejectionCategory, RejectionError


def new_run_id() -> str:
    return uuid.uuid4().hex


def ensure_home(home: Path) -> dict[str, Path]:
    """Create the fixed service directory layout and return key paths."""
    dirs = {
        "home": home,
        "spool": home / "spool",
        "runs": home / "runs",
        "manifests": home / "manifests",
        "audit": home / "audit",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def create_run_dir(runs_root: Path, run_id: str) -> Path:
    """Create ``runs/<run_id>/out`` with restrictive permissions."""
    if not _safe_segment(run_id):
        raise RejectionError(
            RejectionCategory.INTERNAL_ERROR,
            f"illegal run id {run_id!r}",
        )
    run_dir = runs_root / run_id
    out_dir = run_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=False)
    os.chmod(run_dir, 0o700)
    os.chmod(out_dir, 0o700)
    return out_dir


def remove_run_dir(run_dir: Path) -> None:
    """Best-effort removal of a rejected/failed run directory tree."""
    shutil.rmtree(run_dir, ignore_errors=True)


def spool_upload(home: Path, data: bytes, run_id: str) -> Path:
    """Persist uploaded bytes under ``home/spool`` (exclusive, mode 0600)."""
    spool_dir = home / "spool"
    spool_dir.mkdir(parents=True, exist_ok=True)
    path = spool_dir / f"{run_id}.bin"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def remove_spool(path: Path) -> None:
    path.unlink(missing_ok=True)


def assert_within(root: Path, candidate: Path) -> Path:
    """Containment predicate: ``candidate`` must resolve under ``root``.

    Symlinks in ``candidate`` itself are NOT followed (``os.path.commonpath``
    works on lexical, already-normalized paths here); callers that need a
    symlink-resolved check use :func:`realpath_within`.
    """
    try:
        common = os.path.commonpath([root, candidate])
    except ValueError:
        raise RejectionError(
            RejectionCategory.CONTAINMENT_VIOLATION,
            f"path {candidate} is on a different drive than {root}",
        )
    if common != str(root):
        raise RejectionError(
            RejectionCategory.CONTAINMENT_VIOLATION,
            f"resolved path {candidate} escapes isolated root {root}",
        )
    return candidate


def realpath_within(root: Path, candidate: Path) -> Path:
    """Resolve symlinks and require containment under root."""
    resolved = Path(os.path.realpath(candidate))
    root_resolved = Path(os.path.realpath(root))
    try:
        common = os.path.commonpath([root_resolved, resolved])
    except ValueError:
        raise RejectionError(
            RejectionCategory.CONTAINMENT_VIOLATION,
            f"path {resolved} escapes isolated root {root_resolved}",
        )
    if common != str(root_resolved):
        raise RejectionError(
            RejectionCategory.CONTAINMENT_VIOLATION,
            f"resolved path {resolved} escapes isolated root {root_resolved}",
        )
    return resolved


def _safe_segment(segment: str) -> bool:
    return bool(segment) and "/" not in segment and "\\" not in segment and segment not in (".", "..")


def atomic_write_json(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file + rename, mode 0600)."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
