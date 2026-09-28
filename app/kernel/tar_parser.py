"""Plain (uncompressed) TAR evidence parser over the standard-library tarfile.

Opened with mode "r:" so tarfile never auto-decompresses. gzip/bzip2/xz outer
containers are rejected before this point by parser.detect_container.
Long-name (GNU/PAX) headers are resolved transparently by tarfile itself.
"""
from __future__ import annotations

import io
import tarfile
from typing import Callable, Iterator

from .errors import CorruptArchive, UnsupportedEntryType
from .model import EntryEvidence, EntryKind
from .parser import EvidenceParser

class TarEvidenceParser(EvidenceParser):
    def __init__(self, data: bytes):
        super().__init__(data, "tar")
        self._buf = io.BytesIO(data)
        try:
            self._tf = tarfile.open(fileobj=self._buf, mode="r:")
        except tarfile.ReadError as exc:
            raise CorruptArchive(f"invalid tar structure: {exc}") from exc

    def close(self) -> None:
        self._tf.close()

    @staticmethod
    def _classify(member: tarfile.TarInfo) -> EntryKind:
        if member.isdir():
            return EntryKind.DIRECTORY
        if member.isreg():
            return EntryKind.FILE
        if member.issym():
            return EntryKind.SYMLINK
        if member.islnk():
            # Hard links are policy-rejected by the kernel; parser still reports
            # evidence faithfully so the failure category is exact.
            return EntryKind.HARDLINK
        if member.isfifo() or member.isblk() or member.ischr() or member.isdev():
            return EntryKind.SPECIAL
        raise UnsupportedEntryType(
            f"tar member type {member.type!r} is outside the restricted subset",
            evidence=member.name,
        )

    def entries(self) -> Iterator[tuple[EntryEvidence, Callable]]:
        try:
            members = self._tf.getmembers()
        except tarfile.ReadError as exc:
            raise CorruptArchive(f"unreadable tar headers: {exc}") from exc

        for index, member in enumerate(members):
            kind = self._classify(member)
            evidence = EntryEvidence(
                index=index,
                kind=kind,
                raw_name=member.name,
                declared_size=int(member.size),
                mode=member.mode or (0o755 if kind == EntryKind.DIRECTORY else 0o644),
                link_target=member.linkname or None,
            )

            def opener(_member: tarfile.TarInfo = member):
                fh = self._tf.extractfile(_member)
                if fh is None:
                    raise CorruptArchive(
                        "expected payload stream for regular file",
                        evidence=_member.name,
                    )
                return fh

            yield evidence, opener

    def declared_compressed_size(self) -> int:
        # Uncompressed tar: payload occupies size rounded up to 512-byte blocks.
        total = 0
        for member in self._tf.getmembers():
            if member.isreg():
                total += ((int(member.size) + 511) // 512) * 512
        return total
