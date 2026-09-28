"""Extraction budget accounting and policy limits."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .errors import RejectionCategory, RejectionError


@dataclass(frozen=True)
class Budget:
    """Policy limits applied while planning and extracting an archive."""

    max_total_bytes: int
    max_files: int
    max_depth: int
    max_symlink_hops: int
    max_compression_ratio: float

    @classmethod
    def from_config(cls, cfg: dict) -> "Budget":
        return cls(
            max_total_bytes=int(cfg["max_total_bytes"]),
            max_files=int(cfg["max_files"]),
            max_depth=int(cfg["max_depth"]),
            max_symlink_hops=int(cfg["max_symlink_hops"]),
            max_compression_ratio=float(cfg["max_compression_ratio"]),
        )

    def validate(self) -> None:
        checks = {
            "max_total_bytes": self.max_total_bytes > 0,
            "max_files": self.max_files > 0,
            "max_depth": self.max_depth > 0,
            "max_symlink_hops": self.max_symlink_hops > 0,
            "max_compression_ratio": self.max_compression_ratio > 0,
        }
        bad = [name for name, ok in checks.items() if not ok]
        if bad:
            raise RejectionError(
                RejectionCategory.INTERNAL_ERROR,
                f"invalid budget configuration: {', '.join(bad)} must be positive",
            )

    def check_files(self, count: int) -> None:
        if count > self.max_files:
            raise RejectionError(
                RejectionCategory.BUDGET_FILE_COUNT,
                f"archive declares {count} entries; limit is {self.max_files}",
            )

    def check_depth(self, depth: int, entry: str) -> None:
        if depth > self.max_depth:
            raise RejectionError(
                RejectionCategory.BUDGET_DEPTH,
                f"entry depth {depth} exceeds limit {self.max_depth}",
                entry=entry,
            )

    def check_total(self, total: int) -> None:
        if total > self.max_total_bytes:
            raise RejectionError(
                RejectionCategory.BUDGET_TOTAL_BYTES,
                f"declared uncompressed total {total} bytes exceeds limit "
                f"{self.max_total_bytes} bytes",
            )

    def check_ratio(self, compressed: int, uncompressed: int) -> None:
        if compressed <= 0:
            # Empty / directory-only archives have no meaningful ratio.
            return
        ratio = uncompressed / compressed
        if ratio > self.max_compression_ratio:
            raise RejectionError(
                RejectionCategory.BUDGET_RATIO,
                f"declared compression ratio {ratio:.1f}:1 exceeds limit "
                f"{self.max_compression_ratio:g}:1",
            )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Usage:
    """Running counters, updated while scanning the archive."""

    file_count: int = 0
    total_bytes: int = 0
    max_depth: int = 0
    compressed_bytes: int = 0

    def to_dict(self) -> dict:
        ratio = (
            round(self.total_bytes / self.compressed_bytes, 3)
            if self.compressed_bytes
            else 0.0
        )
        return {
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "max_depth": self.max_depth,
            "compressed_bytes": self.compressed_bytes,
            "observed_ratio": ratio,
        }


def dump_budget(budget: Budget) -> str:
    return json.dumps(budget.to_dict(), sort_keys=True)
