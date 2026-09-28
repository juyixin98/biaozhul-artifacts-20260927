"""Format detection and a uniform reader interface over ZIP and TAR.

Only an uncompressed, restricted subset is supported:

* ZIP via :mod:`zipfile`
* plain (uncompressed) POSIX TAR via :mod:`tarfile`; gzip/bzip2/xz are refused
* entry kinds: regular files, directories, symbolic links only
* hard links and special files (fifo, device, socket) are refused
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from dataclasses import dataclass
from enum import Enum

from ..errors import RejectionCategory, RejectionError


class EntryKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    SPECIAL = "special"


class ArchiveFormat(str, Enum):
    ZIP = "zip"
    TAR = "tar"


@dataclass
class Entry:
    """One archive member, with declared (header) metadata only."""

    name: str
    """Raw name exactly as stored (already decoded as UTF-8)."""

    kind: EntryKind
    size: int
    """Declared uncompressed size in bytes."""

    compressed_size: int
    """On-dark/on-wire size (best effort; equal to size for tar)."""

    is_dir: bool = False
    mode: int = 0o644
    index: int = 0

    def kind_label(self) -> str:
        return self.kind.value


@dataclass
class ArchiveInfo:
    fmt: ArchiveFormat
    archive_size: int
    entries: list[Entry]


class ArchiveReader:
    """Context-manager reader; payloads are streamed, never extracted."""

    def __enter__(self) -> "ArchiveReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def entries(self) -> list[Entry]:  # pragma: no cover - overridden
        raise NotImplementedError

    def open_payload(self, entry: Entry):  # pragma: no cover - overridden
        """Return a binary file-like object positioned at the payload start."""
        raise NotImplementedError

    def read_symlink_target(self, entry: Entry) -> str:  # pragma: no cover
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def detect_format(data: bytes) -> ArchiveFormat:
    """Identify the format from magic bytes.

    ZIP: ``PK\\x03\\x04`` (local file header) or ``PK\\x05\\x06`` (empty archive).
    TAR: ustar magic at offset 257, or a 512-byte blocked GNU tar without it.
    """
    is_zip = data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
    is_tar = _looks_like_tar(data)
    if is_zip and is_tar:
        raise RejectionError(
            RejectionCategory.FORMAT_AMBIGUOUS,
            "content matches both ZIP and TAR signatures",
        )
    if is_zip:
        return ArchiveFormat.ZIP
    if is_tar:
        return ArchiveFormat.TAR
    raise RejectionError(
        RejectionCategory.FORMAT_UNSUPPORTED,
        "not a recognized ZIP or uncompressed TAR archive",
    )


def _looks_like_tar(data: bytes) -> bool:
    if len(data) < 263:
        return False
    if data[257:262] == b"ustar":
        return True
    # GNU tar without the ustar prefix: name block NUL-terminated in the first
    # 100 bytes and size field octal-parseable, length a multiple of 512.
    first_block = data[:512]
    if len(data) % 512 != 0:
        return False
    nul = first_block.find(b"\x00")
    if 0 < nul <= 99:
        size_field = first_block[124:136]
        try:
            int(size_field.split(b"\x00", 1)[0].strip() or b"0", 8)
            return True
        except ValueError:
            return False
    return False


def open_reader(data: bytes) -> ArchiveReader:
    fmt = detect_format(data)
    if fmt is ArchiveFormat.ZIP:
        return ZipReader(data)
    return TarReader(data)


# --------------------------------------------------------------------------- #
# ZIP
# --------------------------------------------------------------------------- #

class ZipReader(ArchiveReader):
    def __init__(self, data: bytes) -> None:
        self._data = data
        try:
            self._zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise RejectionError(
                RejectionCategory.ARCHIVE_CORRUPT,
                f"malformed ZIP archive: {exc}",
            ) from exc
        # infolist() forces a full central-directory walk so truncation or a
        # damaged directory is caught here rather than mid-extraction.
        try:
            self._zf.infolist()
        except Exception as exc:  # noqa: BLE001 - zipfile raises several types
            raise RejectionError(
                RejectionCategory.ARCHIVE_CORRUPT,
                f"unreadable ZIP central directory: {exc}",
            ) from exc
        self._entries = self._build_entries()

    def entries(self) -> list[Entry]:
        return self._entries

    def _build_entries(self) -> list[Entry]:
        out: list[Entry] = []
        for i, info in enumerate(self._zf.infolist()):
            kind = self._kind_for(info)
            name = self._decode_name(info)
            if name.endswith("/") and kind is EntryKind.FILE:
                # Directory entry that was not marked by external attrs.
                kind = EntryKind.DIRECTORY
            out.append(
                Entry(
                    name=name,
                    kind=kind,
                    size=info.file_size,
                    compressed_size=max(info.compress_size, 0),
                    is_dir=kind is EntryKind.DIRECTORY,
                    mode=self._mode_for(info),
                    index=i,
                )
            )
        return out

    @staticmethod
    def _decode_name(info: zipfile.ZipInfo) -> str:
        raw = info.filename
        try:
            if info.flag_bits & 0x800:
                return raw
            return raw.encode("cp437").decode("utf-8")
        except (UnicodeError, AttributeError):
            try:
                return raw if isinstance(raw, str) else raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RejectionError(
                    RejectionCategory.ARCHIVE_CORRUPT,
                    f"undecodable ZIP entry name: {exc}",
                ) from exc

    @staticmethod
    def _unix_mode(info: zipfile.ZipInfo) -> int | None:
        # Python 3.12 stores create_system as its own field; on older releases
        # it is the high byte of create_version.
        system = getattr(info, "create_system", None)
        if system is None:
            system = (info.create_version >> 8) & 0xFF
        if system == 3:  # 3 = Unix
            return (info.external_attr >> 16) & 0xFFFF
        return None

    def _mode_for(self, info: zipfile.ZipInfo) -> int:
        mode = self._unix_mode(info)
        return mode if mode is not None else 0o644

    def _kind_for(self, info: zipfile.ZipInfo) -> EntryKind:
        mode = self._unix_mode(info)
        if mode is None:
            # DOS / no Unix attributes: infer from the trailing slash only.
            return EntryKind.DIRECTORY if info.filename.endswith("/") else EntryKind.FILE
        fmt = mode & 0o170000
        if fmt == 0o040000 or info.filename.endswith("/"):
            return EntryKind.DIRECTORY
        if fmt == 0o120000:
            return EntryKind.SYMLINK
        if fmt == 0o100000 or fmt == 0:
            # S_IFREG, or Unix entry with no type bits set (common for files
            # written by naive libraries).
            return EntryKind.FILE
        if fmt == 0o010000:  # FIFO
            return EntryKind.SPECIAL
        # Character/block devices, sockets, contiguous files and hard links
        # are not representable as anything we allow.
        return EntryKind.SPECIAL

    def open_payload(self, entry: Entry):
        info = self._zf.infolist()[entry.index]
        try:
            return self._zf.open(info, "r")
        except (zipfile.BadZipFile, RuntimeError, zlib_error()) as exc:
            raise RejectionError(
                RejectionCategory.ARCHIVE_CORRUPT,
                f"cannot open ZIP payload for {entry.name!r}: {exc}",
                entry=entry.name,
            ) from exc

    def read_symlink_target(self, entry: Entry) -> str:
        with self.open_payload(entry) as fh:
            data = fh.read()
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RejectionError(
                RejectionCategory.SYMLINK_ESCAPE,
                f"non-UTF8 symlink target in {entry.name!r}: {exc}",
                entry=entry.name,
            ) from exc

    def close(self) -> None:
        self._zf.close()


def zlib_error():
    import zlib

    return zlib.error


# --------------------------------------------------------------------------- #
# TAR (uncompressed POSIX subset)
# --------------------------------------------------------------------------- #

class TarReader(ArchiveReader):
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._fileobj = io.BytesIO(data)
        try:
            self._tf = tarfile.open(fileobj=self._fileobj, mode="r:")
        except tarfile.CompressionError as exc:
            raise RejectionError(
                RejectionCategory.FORMAT_UNSUPPORTED,
                f"compressed TAR is not supported: {exc}",
            ) from exc
        except (tarfile.ReadError, tarfile.StreamError, tarfile.HeaderError) as exc:
            raise RejectionError(
                RejectionCategory.ARCHIVE_CORRUPT,
                f"malformed TAR archive: {exc}",
            ) from exc
        self._members = self._read_members()

    def _read_members(self) -> list[tarfile.TarInfo]:
        members: list[tarfile.TarInfo] = []
        try:
            while True:
                member = self._tf.next()
                if member is None:
                    break
                members.append(member)
        except (tarfile.ReadError, tarfile.StreamError, tarfile.HeaderError) as exc:
            msg = str(exc).lower()
            # next() physically skips the prior member's payload; "unexpected
            # end of data" there means the bytes produced < the declared size.
            if "end of data" in msg or "truncated" in msg:
                category = RejectionCategory.DECLARED_SIZE_MISMATCH
                detail = f"payload shorter than its TAR header declares: {exc}"
            else:
                category = RejectionCategory.ARCHIVE_CORRUPT
                detail = f"malformed TAR stream: {exc}"
            raise RejectionError(category, detail) from exc
        return members

    def entries(self) -> list[Entry]:
        out: list[Entry] = []
        for i, m in enumerate(self._members):
            name = self._decode_name(m)
            kind = self._kind_for(m)
            out.append(
                Entry(
                    name=name,
                    kind=kind,
                    size=m.size if m.isreg() else 0,
                    compressed_size=m.size if m.isreg() else 0,
                    is_dir=kind is EntryKind.DIRECTORY,
                    mode=member_mode(m),
                    index=i,
                )
            )
        return out

    @staticmethod
    def _decode_name(m: tarfile.TarInfo) -> str:
        try:
            return m.name
        except UnicodeDecodeError as exc:
            raise RejectionError(
                RejectionCategory.ARCHIVE_CORRUPT,
                f"undecodable TAR entry name: {exc}",
            ) from exc

    @staticmethod
    def _kind_for(m: tarfile.TarInfo) -> EntryKind:
        if m.isdir():
            return EntryKind.DIRECTORY
        if m.isreg():
            return EntryKind.FILE
        if m.issym():
            return EntryKind.SYMLINK
        if m.islnk():
            return EntryKind.HARDLINK
        return EntryKind.SPECIAL  # fifo, chr, blk, socket, contiguous...

    def open_payload(self, entry: Entry):
        m = self._members[entry.index]
        try:
            fh = self._tf.extractfile(m)
        except (tarfile.ReadError, tarfile.StreamError) as exc:
            raise RejectionError(
                RejectionCategory.ARCHIVE_CORRUPT,
                f"cannot open TAR payload for {entry.name!r}: {exc}",
                entry=entry.name,
            ) from exc
        if fh is None:
            raise RejectionError(
                RejectionCategory.ARCHIVE_CORRUPT,
                f"no payload available for {entry.name!r}",
                entry=entry.name,
            )
        return fh

    def read_symlink_target(self, entry: Entry) -> str:
        m = self._members[entry.index]
        raw = m.linkname
        if isinstance(raw, bytes):
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RejectionError(
                    RejectionCategory.SYMLINK_ESCAPE,
                    f"non-UTF8 symlink target in {entry.name!r}: {exc}",
                    entry=entry.name,
                ) from exc
        return raw

    def close(self) -> None:
        self._tf.close()
        self._fileobj.close()


def member_mode(m: tarfile.TarInfo) -> int:
    return m.mode & 0o7777


def archive_size(data: bytes) -> int:
    return len(data)
