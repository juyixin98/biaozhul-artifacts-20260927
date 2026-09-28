"""Independent synthetic archive fixtures.

These builders use only the standard library (zipfile / tarfile / struct) and
hand-crafted bytes.  They are deliberately NOT derived from the code under
test, so the test oracle cannot accidentally agree with the implementation.
"""

from __future__ import annotations

import io
import stat
import struct
import tarfile
import zipfile
from dataclasses import dataclass


# --------------------------------------------------------------------------- #
# ZIP builders
# --------------------------------------------------------------------------- #

@dataclass
class ZipSpec:
    name: str
    kind: str = "file"  # file | dir | symlink
    data: bytes = b""
    target: bytes | None = None  # symlink payload
    system: int = 3  # 3 = Unix


def build_zip(specs: list[ZipSpec], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as z:
        for spec in specs:
            if spec.kind == "file":
                z.writestr(spec.name, spec.data)
            elif spec.kind == "dir":
                z.writestr(spec.name.rstrip("/") + "/", b"")
            elif spec.kind == "symlink":
                info = zipfile.ZipInfo(spec.name)
                info.create_system = spec.system
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                z.writestr(info, spec.target)
            else:  # pragma: no cover
                raise ValueError(spec.kind)
    return buf.getvalue()


def build_zip_with_unix(name: str, unix_mode: int, payload: bytes = b"") -> bytes:
    """ZIP containing one entry carrying arbitrary Unix type bits."""
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (unix_mode << 16) & 0xFFFF0000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr(info, payload)
    return buf.getvalue()


def zip_with_duplicate_name(first: bytes, second: bytes, name: str = "a.txt") -> bytes:
    """A ZIP that stores the same entry twice (writers normally refuse)."""
    import warnings

    out = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
            z.writestr(name, first)
            # Force a second, duplicate member at the zipfile level.
            z.writestr(name, second)
    return out.getvalue()


def zip_declared_size_mismatch(
    payload: bytes = b"hello", declared: int = 99
) -> bytes:
    """Rewrite BOTH size fields (central + local) to a wrong value."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("a.txt", payload)
    raw = bytearray(buf.getvalue())
    cd = raw.find(b"PK\x01\x02")
    lh = raw.find(b"PK\x03\x04")
    struct.pack_into("<I", raw, cd + 24, declared)  # central compressed size
    struct.pack_into("<I", raw, cd + 28, declared)  # central uncompressed size
    struct.pack_into("<I", raw, lh + 18, declared)  # local uncompressed size
    struct.pack_into("<I", raw, lh + 22, declared)  # local compressed size
    return bytes(raw)


def zip_with_bad_crc(payload: bytes = b"hello world") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("a.txt", payload)
    raw = bytearray(buf.getvalue())
    idx = raw.find(payload)
    raw[idx + len(payload) - 1] ^= 0xFF  # flip last payload byte
    return bytes(raw)


# --------------------------------------------------------------------------- #
# TAR builders
# --------------------------------------------------------------------------- #

def build_tar(members: list[tuple]) -> bytes:
    """members: list of (kind, name, extra).

    kind ∈ {"file","dir","symlink","hardlink","fifo","chr"}; extra is payload
    bytes for files or the link target string for links.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:", format=tarfile.USTAR_FORMAT) as t:
        for item in members:
            kind, name = item[0], item[1]
            extra = item[2] if len(item) > 2 else None
            ti = tarfile.TarInfo(name)
            if kind == "file":
                data = extra or b""
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                t.addfile(ti)
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = extra
                t.addfile(ti)
            elif kind == "hardlink":
                ti.type = tarfile.LNKTYPE
                ti.linkname = extra
                t.addfile(ti)
            elif kind == "fifo":
                ti.type = tarfile.FIFOTYPE
                t.addfile(ti)
            elif kind == "chr":
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 1
                ti.devminor = 3
                t.addfile(ti)
            else:  # pragma: no cover
                raise ValueError(kind)
    return buf.getvalue()


def tar_truncated_payload(name: str = "big.bin", declared: int = 200,
                          actual: int = 100) -> bytes:
    """Header says ``declared`` bytes; physically only ``actual`` are present."""
    full = build_tar([("file", name, b"A" * declared)])
    # 512-byte header followed by only `actual` payload bytes; drop the rest.
    return full[: 512 + actual]
