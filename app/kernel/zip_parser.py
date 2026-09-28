"""ZIP evidence parser over the standard-library zipfile module.

Reports header evidence; performs no path or policy decisions. The streaming
read in the extractor re-uses zipfile's CRC-32 verification and we additionally
compare the byte count against the declared uncompressed size.
"""
from __future__ import annotations

import io
import zipfile
import zlib
from typing import Callable, Iterator

from .errors import (
    CorruptArchive,
    UnsupportedCompression,
    UnsupportedEncryption,
)
from .model import EntryEvidence, EntryKind
from .parser import EvidenceParser

# zip external-attribute Unix mode bits.
UNX_IFMT = 0o170000
UNX_IFLNK = 0o120000
UNX_IFDIR = 0o040000

SUPPORTED_METHODS = {0: "stored", 8: "deflated"}


class ZipEvidenceParser(EvidenceParser):
    def __init__(self, data: bytes):
        super().__init__(data, "zip")
        try:
            self._zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise CorruptArchive(f"invalid zip structure: {exc}") from exc

    def close(self) -> None:
        self._zf.close()

    def _classify(self, info: zipfile.ZipInfo) -> tuple[EntryKind, int, str | None]:
        unix_mode = (info.external_attr >> 16) & UNX_IFMT
        perm_mode = (info.external_attr >> 16) & 0xFFFF
        link_target: str | None = None
        if unix_mode == UNX_IFLNK:
            # ZIP stores the link target path as the entry's payload.
            try:
                link_target = self._zf.read(info).decode("utf-8", "surrogateescape")
            except (zipfile.BadZipFile, zlib.error) as exc:
                raise CorruptArchive(
                    f"unreadable symlink target in {info.filename!r}: {exc}"
                ) from exc
            return EntryKind.SYMLINK, perm_mode, link_target
        if info.is_dir() or unix_mode == UNX_IFDIR:
            return EntryKind.DIRECTORY, perm_mode or 0o755, None
        return EntryKind.FILE, perm_mode or 0o644, None

    def entries(self) -> Iterator[tuple[EntryEvidence, Callable]]:
        try:
            infos = self._zf.infolist()
        except zipfile.BadZipFile as exc:
            raise CorruptArchive(f"unreadable central directory: {exc}") from exc

        for index, info in enumerate(infos):
            if info.flag_bits & 0x1:
                raise UnsupportedEncryption(
                    "encrypted zip entries are outside the restricted subset",
                    evidence=info.filename,
                )
            if info.compress_type not in SUPPORTED_METHODS:
                raise UnsupportedCompression(
                    f"zip method {info.compress_type} is not supported "
                    f"(only stored=0 and deflated=8)",
                    evidence=info.filename,
                )
            kind, mode, link_target = self._classify(info)
            evidence = EntryEvidence(
                index=index,
                kind=kind,
                raw_name=info.filename,
                declared_size=int(info.file_size),
                mode=mode,
                link_target=link_target,
                compress_size=int(info.compress_size),
                compress_type=info.compress_type,
            )

            def opener(_info: zipfile.ZipInfo = info):
                # zipfile verifies CRC-32 when this stream reaches EOF.
                return self._zf.open(_info, "r")

            yield evidence, opener

    def declared_compressed_size(self) -> int:
        return sum(int(i.compress_size) for i in self._zf.infolist())
