"""Controlled extraction executor.

Pre-conditions (guaranteed by canonical.build_plan):
  * every destination is inside the run root;
  * symlink targets are in-root and acyclic;
  * no two entries share a physical path;
  * declared totals fit the budgets.

Runtime defence in depth:
  * directories: O_DIRECTORY-free ``mkdir`` with EEXIST race check;
  * symlinks: ``os.symlink`` with the raw, already-validated target;
  * regular files: ``os.open`` with O_CREAT|O_EXCL|O_NOFOLLOW, streamed in
    64 KiB chunks, counting bytes against the *declared* size and the live
    total budget; CRC-32 (zip) failures surface from the payload stream;
  * any exception wipes the partial output directory — rejection leaves
    nothing behind.
"""
from __future__ import annotations

import errno
import os
import shutil
import tarfile
import zipfile
import zlib
from pathlib import Path

from ..config import Budgets
from .canonical import Plan
from .errors import (
    ArchiveError,
    CompressionBomb,
    CorruptArchive,
    DeclaredLengthMismatch,
    IntegrityFailure,
)

CHUNK = 64 * 1024


def _map_payload_error(exc: Exception, evidence: str) -> ArchiveError:
    """Map streaming exceptions to exact failure categories."""
    if isinstance(exc, ArchiveError):
        return exc
    if isinstance(exc, zlib.error):
        # Deflate stream corruption. zipfile usually converts CRC errors to
        # BadZipFile itself; a raw zlib error is payload corruption.
        return IntegrityFailure(f"deflate stream corrupt: {exc}", evidence=evidence)
    if isinstance(exc, zipfile.BadZipFile):
        text = str(exc).lower()
        if "crc" in text or "checksum" in text:
            return IntegrityFailure(f"crc-32 mismatch: {exc}", evidence=evidence)
        # Truncated / length problems at decompression time.
        return DeclaredLengthMismatch(
            f"payload length problem: {exc}", evidence=evidence
        )
    if isinstance(exc, tarfile.ReadError):
        text = str(exc).lower()
        if "unexpected end" in text or "truncated" in text:
            return DeclaredLengthMismatch(
                f"tar payload truncated: {exc}", evidence=evidence
            )
        return CorruptArchive(f"unreadable tar payload: {exc}", evidence=evidence)
    if isinstance(exc, EOFError):
        return DeclaredLengthMismatch(
            f"payload ended before declared length: {exc}", evidence=evidence
        )
    if isinstance(exc, OSError) and exc.errno in {
        errno.ELOOP,
        errno.ENOTDIR,
        errno.EEXIST,
        errno.ENOENT,
    }:
        # With the plan validated, an ELOOP/EEXIST here indicates the on-disk
        # state no longer matches the plan — fail closed as corruption.
        return CorruptArchive(
            f"filesystem state violated the validated plan: {exc}",
            evidence=evidence,
        )
    return CorruptArchive(f"unexpected extraction failure: {exc}", evidence=evidence)


def _write_regular_file(
    root: Path,
    action,
    budgets: Budgets,
    live_total: int,
) -> tuple[int, int, int | None]:
    """Write one regular file.

    Returns (written_bytes, stored_bytes, compress_type) where stored_bytes is
    the number of payload bytes occupied inside the archive (zip compress_size).
    """
    stored_bytes = action.compress_size or 0
    compress_type = action.compress_type
    dest = root.joinpath(*action.physical)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Re-check the resolved parent is a real directory inside the root.
    parent_real = os.path.realpath(dest.parent)
    root_real = os.path.realpath(root)
    if parent_real != root_real and not parent_real.startswith(root_real + os.sep):
        raise IntegrityFailure(  # fail closed; plan said this was in-root
            "resolved parent escaped the run root",
            evidence=str(dest),
        )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(dest, flags, action.mode & 0o777)
    written = 0
    overshoot = False
    try:
        stream = action.opener()
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                # Reaching EOF triggers zipfile's CRC-32 verification, which
                # raises BadZipFile("Bad CRC-32...") on mismatch.
                break
            written += len(chunk)
            if written > action.declared_size:
                # Never write attacker-controlled excess bytes to disk.
                overshoot = True
                break
            if live_total + written > budgets.max_total_uncompressed_bytes:
                from .errors import BudgetTotalSizeExceeded

                raise BudgetTotalSizeExceeded(
                    f"runtime total {live_total + written} exceeded budget "
                    f"{budgets.max_total_uncompressed_bytes}",
                    evidence=f"{'/'.join(action.canonical)} cumulative={live_total + written}",
                )
            os.write(fd, chunk)
    except ArchiveError:
        raise
    except Exception as exc:  # payload-level corruption / checksum failure
        raise _map_payload_error(exc, "/".join(action.canonical)) from exc
    finally:
        os.close(fd)

    if overshoot or written != action.declared_size:
        raise DeclaredLengthMismatch(
            f"declared {action.declared_size} bytes but extracted {written}",
            evidence=f"{'/'.join(action.canonical)} declared={action.declared_size} actual={written}",
        )
    return written, stored_bytes, compress_type


def execute_plan(plan: Plan, output_dir: Path, budgets: Budgets) -> dict:
    """Execute a validated plan inside ``output_dir``.

    Returns a manifest dict. Raises ArchiveError; on failure the output
    directory is removed entirely (atomic-ish rollback for a fresh run dir).
    """
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    try:
        os.chmod(root, 0o700)
        live_total = 0
        manifest_entries: list[dict] = []

        for action in plan.actions:
            rel = "/".join(action.canonical)
            if action.kind == "mkdir":
                target = root.joinpath(*action.physical)
                try:
                    os.mkdir(target)
                    os.chmod(target, 0o755)
                except FileExistsError:
                    # Must be a directory, never a symlink/file.
                    st = os.lstat(target)
                    import stat as stat_mod

                    if not stat_mod.S_ISDIR(st.st_mode) or stat_mod.S_ISLNK(st.st_mode):
                        raise CorruptArchive(
                            "directory target exists and is not a real directory",
                            evidence=rel,
                        )
            elif action.kind == "symlink":
                target = root.joinpath(*action.canonical)
                target.parent.mkdir(parents=True, exist_ok=True)
                # EEXIST would mean a placement the plan said was free; O_NOFOLLOW
                # is implicit for symlink creation (symlink never follows).
                os.symlink(action.link_target, target)
            else:  # write
                n, stored, compress_type = _write_regular_file(
                    root, action, budgets, live_total
                )
                live_total += n
                # Exact bomb check against bytes ACTUALLY decompressed. Only
                # meaningful for compressed payloads (zip deflate=8). A lying
                # header that claimed a giant size but yielded few bytes never
                # reaches here: it failed declared_length_mismatch above.
                if compress_type == 8 and stored > 0:
                    ratio = n / stored
                    if ratio > budgets.max_compression_ratio:
                        raise CompressionBomb(
                            f"actual compression ratio {ratio:.1f}x exceeds "
                            f"budget {budgets.max_compression_ratio}x",
                            evidence=(
                                f"{'/'.join(action.canonical)} "
                                f"actual={n}B stored={stored}B"
                            ),
                        )
                manifest_entries.append(
                    {
                        "name": "/".join(action.canonical),
                        "physical": "/".join(action.physical),
                        "size": n,
                    }
                )

        return {
            "entries_written": len(manifest_entries),
            "total_bytes": live_total,
            "files": manifest_entries,
            "directories": ["/".join(d) for d in sorted(plan.directories)],
            "symlinks": {"/".join(k): v for k, v in plan.symlinks.items()},
        }
    except BaseException:
        # Roll back ALL partial state inside the isolated directory.
        shutil.rmtree(root, ignore_errors=True)
        raise
