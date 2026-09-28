"""Controlled extraction kernel.

After the path graph has been proven safe (see :mod:`archguard.paths`), the
kernel materializes entries in a fixed order — directories, symbolic links,
then regular files — under the isolated run directory.

Safety properties enforced during extraction:

* payload bytes are streamed and counted; short reads and trailing bytes are
  both classified as ``DECLARED_SIZE_MISMATCH``
* ZIP CRC32 failures are classified as ``CONTENT_CRC_MISMATCH``
* every write target is re-checked against the root with symlink resolution,
  so a pre-existing symlink inside the run dir cannot redirect writes
* no mode bits are honored (files 0600, dirs 0700, symlinks as created)
* an independent recursive walk after extraction recomputes type/size/sha256
  for every node and fails the run on any discrepancy (``VERIFY_FAILED``)
"""

from __future__ import annotations

import hashlib
import os
import tarfile
import zlib
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .archiveio import ArchiveReader
from .errors import RejectionCategory, RejectionError
from .isolation import assert_within, realpath_within
from .paths import Node, NodeKind, Plan

_COPY_CHUNK = 64 * 1024


@dataclass
class ExtractedFile:
    declared_path: str
    physical_path: str
    size: int
    sha256: str
    kind: str


def _on_disk_parts(root: Path, parts: tuple[str, ...]) -> Path:
    candidate = root.joinpath(*parts)
    assert_within(root, candidate)
    return candidate


def extract(
    reader: ArchiveReader,
    plan: Plan,
    out_dir: Path,
    audit,
    run_id: str,
) -> list[ExtractedFile]:
    """Materialize the planned entries.  Raises on any violation.

    Order is deliberate: real directories first, then regular files at their
    *physical* (post-symlink-resolution) paths, then symbolic links.  Creating
    links last means no write ever passes through a freshly created symlink.
    """
    root = Path(os.path.realpath(out_dir))
    extracted: list[ExtractedFile] = []

    # Phase 1: real directories (shortest paths first so parents exist).
    for node in plan.directories():
        target = _on_disk_parts(root, node.parts)
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        realpath_within(root, target)

    # Phase 2: regular files at resolved physical paths.  The planner proved
    # each physical path has exactly one owning node.
    total_written = 0
    for node in plan.files():
        physical_parts = plan.link_targets.get(node.parts, node.parts)
        target = _on_disk_parts(root, physical_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Defense in depth: refuse to write through an existing symlink.
        if target.is_symlink():
            raise RejectionError(
                RejectionCategory.CONTAINMENT_VIOLATION,
                f"write target {node.relpath!r} is a symlink",
                entry=node.relpath,
            )
        entry = _entry_for(reader, node)
        written, digest = _stream_file(reader, entry, target, node, audit, run_id)
        total_written += written
        realpath_within(root, target)
        extracted.append(
            ExtractedFile(
                declared_path=node.relpath,
                physical_path="/".join(physical_parts),
                size=written,
                sha256=digest,
                kind="file",
            )
        )
        audit.event(
            run_id,
            "extract",
            "file_written",
            detail={
                "entry": node.relpath,
                "physical": "/".join(physical_parts),
                "size": written,
                "sha256": digest,
            },
        )

    # Phase 3: symbolic links at their declared paths.  Parent directories
    # (real ones) already exist; the link itself must not exist.
    for node in plan.symlinks():
        link_path = _on_disk_parts(root, node.parts)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.exists() or link_path.is_symlink():
            raise RejectionError(
                RejectionCategory.PATH_CONFLICT,
                f"symlink destination {node.relpath!r} already exists",
                entry=node.relpath,
            )
        os.symlink(node.link_text, link_path)
        realpath_within(root, link_path)

    # Phase 4: now that every target exists on disk, every link must resolve
    # to a real, contained, non-dangling path.
    for node in plan.symlinks():
        link_path = _on_disk_parts(root, node.parts)
        physical = realpath_within(root, link_path)
        if not physical.exists():
            raise RejectionError(
                RejectionCategory.VERIFY_FAILED,
                f"created symlink {node.relpath!r} does not resolve on disk",
                entry=node.relpath,
            )

    audit.event(
        run_id,
        "extract",
        "streaming_complete",
        detail={"total_written": total_written, "files": len(extracted)},
    )
    return extracted


def _entry_for(reader: ArchiveReader, node: Node):
    for e in reader.entries():
        if e.index == node.index:
            return e
    raise RejectionError(
        RejectionCategory.INTERNAL_ERROR,
        f"planned entry index {node.index} ({node.relpath!r}) missing from reader",
        entry=node.relpath,
    )


def _stream_file(
    reader: ArchiveReader,
    entry,
    target: Path,
    node: Node,
    audit,
    run_id: str,
) -> tuple[int, str]:
    """Stream one payload to ``target`` with exact-size and CRC enforcement."""
    expected = node.size
    hasher = hashlib.sha256()
    written = 0
    payload = reader.open_payload(entry)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as out:
                while True:
                    try:
                        chunk = payload.read(_COPY_CHUNK)
                    except (
                        zlib.error,
                        zipfile.BadZipFile,
                        EOFError,
                        tarfile.ReadError,
                        tarfile.StreamError,
                    ) as exc:
                        raise _payload_error(exc, node) from exc
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > expected:
                        raise RejectionError(
                            RejectionCategory.DECLARED_SIZE_MISMATCH,
                            f"payload for {node.relpath!r} exceeds declared size "
                            f"{expected} (saw {written} bytes before end)",
                            entry=node.relpath,
                        )
                    hasher.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            # zipfile verifies the CRC-32 once all payload bytes were consumed;
            # close() propagates a BadZipFile if the checksum disagreed.
            try:
                payload.close()
            except (zlib.error, zipfile.BadZipFile) as exc:
                raise _payload_error(exc, node) from exc
            payload = None
        except BaseException:
            # A partial file must never remain as if extracted.
            target.unlink(missing_ok=True)
            raise
    finally:
        if payload is not None:
            try:
                payload.close()
            except Exception:  # noqa: BLE001
                pass

    if written != expected:
        target.unlink(missing_ok=True)
        raise RejectionError(
            RejectionCategory.DECLARED_SIZE_MISMATCH,
            f"payload for {node.relpath!r} produced {written} bytes; header "
            f"declared {expected}",
            entry=node.relpath,
        )
    return written, hasher.hexdigest()


def _payload_error(exc: BaseException, node: Node) -> RejectionError:
    """Map a streaming exception to the precise failure category."""
    msg = str(exc).lower()
    if "crc" in msg or isinstance(exc, zlib.error) and "crc" in msg:
        category = RejectionCategory.CONTENT_CRC_MISMATCH
    elif isinstance(exc, zlib.error):
        # Corrupted compressed stream before the checksum stage.
        category = RejectionCategory.ARCHIVE_CORRUPT
    elif "truncated" in msg or "end of data" in msg or isinstance(exc, EOFError):
        category = RejectionCategory.DECLARED_SIZE_MISMATCH
    else:
        category = RejectionCategory.ARCHIVE_CORRUPT
    return RejectionError(
        category,
        f"payload error for {node.relpath!r}: {exc}",
        entry=node.relpath,
    )


# --------------------------------------------------------------------------- #
# Independent post-extraction verification
# --------------------------------------------------------------------------- #

def verify_tree(
    plan: Plan,
    out_dir: Path,
    extracted: list[ExtractedFile],
    audit,
    run_id: str,
) -> dict:
    """Independently walk the real output tree (never following symlinks) and
    compare it one-to-one against expectations derived from the plan.

    Raises ``VERIFY_FAILED`` on any extra, missing or differing node.
    """
    root = Path(os.path.realpath(out_dir))
    seen_files: dict[str, ExtractedFile] = {f.physical_path: f for f in extracted}

    expected_files: dict[tuple[str, ...], Node] = {}
    expected_links: dict[tuple[str, ...], Node] = {}
    expected_dirs: set[tuple[str, ...]] = set()

    for parts, node in plan.nodes.items():
        if node.kind is NodeKind.DIR:
            expected_dirs.add(parts)
        elif node.kind is NodeKind.FILE:
            physical = plan.link_targets.get(parts, parts)
            expected_files[physical] = node
            _add_parents(expected_dirs, physical)
        elif node.kind is NodeKind.SYMLINK:
            expected_links[parts] = node
            _add_parents(expected_dirs, parts)

    def fail(msg: str, entry: str | None = None) -> None:
        audit.event(
            run_id, "verify", "mismatch", detail={"entry": entry, "reason": msg},
            level=40,
        )
        raise RejectionError(RejectionCategory.VERIFY_FAILED, msg, entry=entry)

    on_disk_files: set[tuple[str, ...]] = set()
    on_disk_links: set[tuple[str, ...]] = set()
    on_disk_dirs: set[tuple[str, ...]] = set()

    def walk(current: Path, parts: tuple[str, ...]) -> None:
        with os.scandir(current) as it:
            for dirent in it:
                child = parts + (dirent.name,)
                full = Path(dirent.path)
                # Containment check on the lexical path and, for links, on the
                # resolved target as well.
                assert_within(root, full)

                if dirent.is_symlink():
                    on_disk_links.add(child)
                    node = expected_links.get(child)
                    if node is None:
                        fail(f"unexpected symlink on disk: {'/'.join(child)!r}")
                    target_text = os.readlink(full)
                    if target_text != node.link_text:
                        fail(
                            f"symlink {'/'.join(child)!r} target "
                            f"{target_text!r} != planned {node.link_text!r}",
                            "/".join(child),
                        )
                    resolved = Path(os.path.realpath(full))
                    realpath_within(root, resolved)
                    if not resolved.exists():
                        fail(
                            f"dangling symlink on disk: {'/'.join(child)!r}",
                            "/".join(child),
                        )
                elif dirent.is_dir(follow_symlinks=False):
                    on_disk_dirs.add(child)
                    if child not in expected_dirs:
                        fail(
                            f"unexpected directory on disk: {'/'.join(child)!r}",
                            "/".join(child),
                        )
                    walk(full, child)
                elif dirent.is_file(follow_symlinks=False):
                    on_disk_files.add(child)
                    node = expected_files.get(child)
                    if node is None:
                        fail(
                            f"unexpected file on disk: {'/'.join(child)!r}",
                            "/".join(child),
                        )
                    st = dirent.stat(follow_symlinks=False)
                    if st.st_size != node.size:
                        fail(
                            f"file {'/'.join(child)!r} size {st.st_size} != "
                            f"planned {node.size}",
                            "/".join(child),
                        )
                    record = seen_files.get("/".join(child))
                    digest = _sha256_of(full)
                    if record is None or record.sha256 != digest:
                        fail(
                            f"sha256 mismatch for {'/'.join(child)!r}",
                            "/".join(child),
                        )
                else:
                    fail(
                        f"non-regular node type on disk: {'/'.join(child)!r}",
                        "/".join(child),
                    )

    walk(root, ())

    missing_files = set(expected_files) - on_disk_files
    if missing_files:
        fail(f"planned files missing on disk: {_fmt(missing_files)}")
    missing_links = set(expected_links) - on_disk_links
    if missing_links:
        fail(f"planned symlinks missing on disk: {_fmt(missing_links)}")
    missing_dirs = expected_dirs - on_disk_dirs
    # Empty leaf directories expected but absent are a real discrepancy.
    leaf_missing = {
        d
        for d in missing_dirs
        if not any(len(o) > len(d) and o[: len(d)] == d for o in on_disk_dirs)
        and not any(len(f) > len(d) and f[: len(d)] == d for f in on_disk_files | on_disk_links)
    }
    if leaf_missing:
        fail(f"planned directories missing on disk: {_fmt(leaf_missing)}")

    result = {
        "files": len(on_disk_files),
        "symlinks": len(on_disk_links),
        "directories": len(on_disk_dirs),
    }
    audit.event(run_id, "verify", "tree_ok", detail=result)
    return result


def _add_parents(dirs: set[tuple[str, ...]], parts: tuple[str, ...]) -> None:
    for depth in range(1, len(parts)):
        dirs.add(parts[:depth])


def _fmt(paths: set[tuple[str, ...]]) -> str:
    return ", ".join("/".join(p) for p in sorted(paths)[:5])


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_COPY_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()
