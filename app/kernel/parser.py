"""Format detection and evidence parser dispatch (ZIP / restricted TAR subset).

Restricted subset:
  * ZIP: stored (0) or deflate (8) compression; no encryption; entries of
    regular file or directory; Unix-mode 0o120000 entries treated as symlinks.
  * TAR: plain ustar/pax/gnu tar only — gzip/bzip2/xz/lzma streams rejected.
No single-file stub: detection and per-format logic live in separate modules.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Iterator

from .errors import UnsupportedCompression, UnsupportedFormat
from .model import EntryEvidence

ZIP_MAGIC = b"PK\x03\x04"
# Local file header and EOCD both start with PK; empty zip has EOCD PK\x05\x06.
ZIP_EOCD_MAGIC = b"PK\x05\x06"
ZIP_CENTRAL_MAGIC = b"PK\x01\x02"

# Classic tar: 512-byte zero blocks begin the end-of-archive marker.
TAR_USTAR_MAGIC = b"ustar"
GNU_MAGIC = b"ustar "


def detect_container(head: bytes) -> str:
    """Identify the outer container from the leading bytes.

    Returns "zip", "tar", "tar.gz", "tar.bz2", "tar.xz" — callers map the
    compressed tar variants to UnsupportedCompression.
    """
    if head.startswith(ZIP_MAGIC) or head.startswith(ZIP_CENTRAL_MAGIC) or head.startswith(ZIP_EOCD_MAGIC):
        return "zip"
    if head[:2] == b"\x1f\x8b":
        return "tar.gz"
    if head[:3] == b"BZh":
        return "tar.bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "tar.xz"
    # ustar magic lives at offset 257; gnu tar uses "ustar  \0", pax uses "ustar\0".
    if len(head) >= 263 and (b"ustar" in head[257:263]):
        return "tar"
    raise UnsupportedFormat(
        "not a supported archive: expected ZIP or plain TAR magic bytes",
        evidence=head[:8].hex(),
    )


def require_plain_tar(container: str) -> None:
    if container == "tar.gz":
        raise UnsupportedCompression(
            "gzip-compressed tar is outside the local restricted subset",
            evidence="gzip magic 1f 8b",
        )
    if container == "tar.bz2":
        raise UnsupportedCompression(
            "bzip2-compressed tar is outside the restricted subset",
            evidence="bzip2 magic 42 5a 68",
        )
    if container == "tar.xz":
        raise UnsupportedCompression(
            "xz-compressed tar is outside the restricted subset",
            evidence="xz magic fd 37 7a",
        )


class EvidenceParser:
    """Context-manager parser yielding EntryEvidence + payload opener factory."""

    def __init__(self, data: bytes, container: str):
        self.data = data
        self.container = container

    def __enter__(self) -> "EvidenceParser":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        pass

    def declared_compressed_size(self) -> int:
        """Total bytes occupied by stored payloads inside the archive."""
        raise NotImplementedError

    def entries(self) -> Iterator[tuple[EntryEvidence, callable]]:
        """Yield (evidence, payload_opener) for every member, in archive order."""
        raise NotImplementedError


def open_parser(data: bytes) -> EvidenceParser:
    container = detect_container(data[:512])
    require_plain_tar(container)
    if container == "zip":
        from .zip_parser import ZipEvidenceParser

        return ZipEvidenceParser(data)
    from .tar_parser import TarEvidenceParser

    return TarEvidenceParser(data)


def parse_file(path: str | Path) -> bytes:
    return Path(path).read_bytes()


def as_bytes(data: bytes | io.BytesIO) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, io.BytesIO):
        return data.getvalue()
    raise UnsupportedFormat("parser input must be bytes or BytesIO")
