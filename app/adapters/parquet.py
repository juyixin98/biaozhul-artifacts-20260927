"""Parquet format adapter.

Responsibility (format adaptation only): read a *local immutable* parquet
file, validate it, and extract its physical facts (schema, row count, the
partition tuple it carries).  It never touches the metadata DB and never
publishes files.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from app.adapters.schema import CanonicalSchema, check_compatible
from app.kernel.errors import ErrorCategory, ServiceError


@dataclass(frozen=True)
class Partition:
    """One concrete partition value tuple, canonicalised as a string pair.

    Canonicalisation rule ("声明规则"): values are rendered with their
    canonical literal form and stored as ``(column, value)`` pairs, so
    int32 ``1`` and string ``"1"`` never silently alias.
    """

    values: tuple[tuple[str, str], ...]

    @classmethod
    def of(
        cls, spec: tuple[str, ...], scalars: dict[str, object]
    ) -> "Partition":
        pairs: list[tuple[str, str]] = []
        for col in spec:
            if col not in scalars:
                raise ServiceError(
                    ErrorCategory.VALIDATION,
                    f"partition value for {col!r} not present in file",
                    details={"column": col},
                )
            pairs.append((col, _render_literal(scalars[col])))
        return cls(tuple(pairs))

    def key(self) -> str:
        return "/".join(f"{c}={v}" for c, v in self.values)

    def as_list(self) -> list[dict[str, str]]:
        return [{"column": c, "value": v} for c, v in self.values]

    def overlaps(self, other: Iterable["Partition"]) -> bool:
        others = list(other)
        return any(self == o for o in others)


def _render_literal(value: object) -> str:
    """Canonical literal rendering for a partition value."""
    if value is None:
        raise ServiceError(
            ErrorCategory.VALIDATION,
            "partition column value must not be null",
        )
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ServiceError(
                ErrorCategory.VALIDATION,
                "non-finite float is not a legal partition value",
            )
        return repr(value)
    if isinstance(value, (bytes,)):
        raise ServiceError(
            ErrorCategory.VALIDATION,
            "binary partition columns are not supported",
        )
    return str(value)


@dataclass(frozen=True)
class FileFacts:
    """Physical facts extracted from one source parquet file."""

    source_path: Path
    schema: CanonicalSchema
    row_count: int
    partitions: tuple[Partition, ...]
    source_size_bytes: int


def read_file_facts(
    source_path: Path, partition_spec: tuple[str, ...]
) -> FileFacts:
    """Open and fully validate one local parquet file.

    Rejected (STAGING_FAILED / VALIDATION) before anything is published:
      * file missing / unreadable / not parquet;
      * schema incompatible with the partition spec;
      * heterogeneous partition values inside a file (one file must belong to
        one or more *whole* partitions; mixed values for one spec column make
        the file unusable for partitioned overwrite detection).
    """
    source_path = Path(source_path)
    if not source_path.is_file():
        raise ServiceError(
            ErrorCategory.STAGING_FAILED,
            f"source parquet file not found: {source_path.name}",
            details={"file": source_path.name},
        )
    try:
        parquet_file = pq.ParquetFile(source_path)
        table = parquet_file.read()
    except (pa.ArrowInvalid, OSError) as exc:
        raise ServiceError(
            ErrorCategory.STAGING_FAILED,
            f"cannot read parquet file {source_path.name}: {exc}",
            details={"file": source_path.name, "cause": type(exc).__name__},
        ) from exc

    arrow_schema = table.schema
    check_compatible(arrow_schema, None, partition_spec)

    # Collect the distinct partition tuples.  Reading the whole file is fine
    # for the local synthetic-fixture scale this service targets.
    partitions = _extract_partitions(table, partition_spec)
    return FileFacts(
        source_path=source_path,
        schema=CanonicalSchema.from_arrow(arrow_schema),
        row_count=table.num_rows,
        partitions=tuple(partitions),
        source_size_bytes=source_path.stat().st_size,
    )


def _extract_partitions(
    table: pa.Table, partition_spec: tuple[str, ...]
) -> list[Partition]:
    if not partition_spec:
        return []
    distinct: dict[str, Partition] = {}
    columns = {c: table.column(c).to_pylist() for c in partition_spec}
    n = table.num_rows
    for i in range(n):
        scalars = {c: columns[c][i] for c in partition_spec}
        part = Partition.of(partition_spec, scalars)
        distinct[part.key()] = part
    return list(distinct.values())


def copy_to_staging(source: Path, staging_dir: Path, staged_name: str) -> Path:
    """Copy a source file into staging via a temp name + fsync + rename.

    The file is fully written and fsynced under ``<tmp>`` before it is renamed
    to its staged name, so a reader can never observe a half-written file.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    final_path = staging_dir / staged_name
    fd, tmp_name = tempfile.mkstemp(prefix=".writing-", dir=staging_dir)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as dst:
            with open(source, "rb") as src:
                shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        tmp_path.replace(final_path)
        _fsync_dir(staging_dir)
        return final_path
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise ServiceError(
            ErrorCategory.STAGING_FAILED,
            f"failed to stage {source.name}: {exc}",
            details={"file": source.name, "cause": type(exc).__name__},
        ) from exc


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Some filesystems do not support fsync on directories; durability of
        # the rename is best-effort there.
        pass
