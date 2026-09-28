"""Entry evidence — the parser's neutral report for one archive member.

Parsers produce *evidence only*: what the archive headers declare. The security
kernel decides what is safe. Parsers never touch the output directory.
"""
from __future__ import annotations

import enum
from typing import Callable

from pydantic import BaseModel


class EntryKind(str, enum.Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    SPECIAL = "special"  # fifo / device / char / block / other unsupported type


class EntryEvidence(BaseModel):
    index: int
    kind: EntryKind
    raw_name: str
    declared_size: int
    mode: int = 0o644
    link_target: str | None = None  # symlink/hardlink target as declared in header
    compress_size: int | None = None  # bytes occupied inside the archive (zip)
    compress_type: int | None = None  # zip compression method code

    model_config = {"arbitrary_types_allowed": True}


# Callable returning a fresh binary stream of the *payload* for a regular file.
PayloadOpener = Callable[[], "object"]
