"""Independent synthetic fixture builders.

These fixtures are constructed with the Python *standard library only*
(zipfile/tarfile plus hand byte-patching) and the expected verdict of each is
hard-coded here — never derived from the kernel under test. That keeps the
oracle independent: the tests assert specific categories the fixtures were
deliberately built to trigger.
"""
from __future__ import annotations

import io
import os
import struct
import tarfile
import zipfile

# ---------------------------------------------------------------------------
# ZIP helpers
# ---------------------------------------------------------------------------
def zip_bytes(entries: list[dict], *, compression=zipfile.ZIP_DEFLATED) -> bytes:
    """Build a zip from [{name, data?, mode?, link?}] descriptors.

    Regular entries: data bytes. Directory: is_dir True. Symlink: link target
    string written as payload with Unix S_IFLNK mode bits.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for e in entries:
            name = e["name"]
            mode = e.get("mode", 0o644)
            if e.get("is_dir"):
                info = zipfile.ZipInfo(name + ("/" if not name.endswith("/") else ""))
                info.external_attr = (0o040000 | 0o755) << 16
                zf.writestr(info, b"")
                continue
            if "link" in e:
                target = e["link"].encode("utf-8")
                info = zipfile.ZipInfo(name)
                # S_IFLNK | rwxrwxrwx so zip parsers recognize a symlink.
                info.external_attr = (0o120000 | 0o777) << 16
                zf.writestr(info, target, compress_type=compression)
                continue
            data = e.get("data", b"")
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100000 | mode) << 16  # S_IFREG
            zf.writestr(info, data, compress_type=compression)
    return buf.getvalue()


def find_local_header(data: bytes, name: str) -> int:
    needle = name.encode("utf-8")
    idx = data.find(b"PK\x03\x04")
    while idx != -1:
        n_len = struct.unpack_from("<H", data, idx + 26)[0]
        x_len = struct.unpack_from("<H", data, idx + 28)[0]
        fname = data[idx + 30 : idx + 30 + n_len]
        if fname == needle:
            return idx
        idx = data.find(b"PK\x03\x04", idx + 4)
    raise AssertionError(f"local header for {name!r} not found")


def find_central_header(data: bytes, name: str) -> int:
    needle = name.encode("utf-8")
    idx = data.find(b"PK\x01\x02")
    while idx != -1:
        n_len = struct.unpack_from("<H", data, idx + 28)[0]
        fname = data[idx + 46 : idx + 46 + n_len]
        if fname == needle:
            return idx
        idx = data.find(b"PK\x01\x02", idx + 4)
    raise AssertionError(f"central header for {name!r} not found")


def zip_with_declared_size_mismatch(name: str = "a.txt", payload: bytes = b"hello") -> bytes:
    """A valid zip whose header-declared *uncompressed* size lies.

    Patched identically in the local and central headers so the kernel's
    evidence count disagrees with the bytes the decompressor yields.
    """
    data = bytearray(zip_bytes([{"name": name, "data": payload}]))
    fake = len(payload) + 999
    lh = find_local_header(bytes(data), name)
    struct.pack_into("<I", data, lh + 22, fake)  # local uncompressed size
    ch = find_central_header(bytes(data), name)
    struct.pack_into("<I", data, ch + 24, fake)  # central uncompressed size
    return bytes(data)


def zip_with_crc_mismatch(name: str = "a.txt", payload: bytes = b"hello") -> bytes:
    """Valid zip whose *central* CRC-32 is wrong (payload untouched).

    zipfile only cross-checks the central CRC at extraction, so this reaches
    streaming extraction and must fail there as integrity_failure.
    """
    data = bytearray(zip_bytes([{"name": name, "data": payload}]))
    ch = find_central_header(bytes(data), name)
    crc = struct.unpack_from("<I", data, ch + 16)[0]
    struct.pack_into("<I", data, ch + 16, crc ^ 0xDEADBEEF)
    return bytes(data)


def zip_truncated(name: str = "a.txt", payload: bytes = b"x" * 4096) -> bytes:
    """Central directory removed -> corrupt archive at parse time."""
    data = zip_bytes([{"name": name, "data": payload}])
    cut = data.find(b"PK\x01\x02")
    return data[:cut]


def zip_stored_bomb_headers(declared_size: int = 200_000_000) -> bytes:
    """A single-entry zip whose headers claim a huge uncompressed size.

    Payload is tiny; the declared total-size budget rejects it before any byte
    is extracted. Uses STORED so there is no real compressed stream to read.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        info = zipfile.ZipInfo("bomb.bin")
        info.external_attr = (0o100000 | 0o644) << 16
        # Trick: write tiny data then patch declared size afterwards.
        zf.writestr(info, b"small")
    data = bytearray(buf.getvalue())
    lh = find_local_header(bytes(data), "bomb.bin")
    ch = find_central_header(bytes(data), "bomb.bin")
    struct.pack_into("<I", data, lh + 22, declared_size)
    struct.pack_into("<I", data, ch + 24, declared_size)
    return bytes(data)


def zip_encryption_flag(name: str = "a.txt") -> bytes:
    """Zip with the encryption bit set (flag bit 0) — must be unsupported."""
    data = bytearray(zip_bytes([{"name": name, "data": b"abc"}]))
    lh = find_local_header(bytes(data), name)
    flags = struct.unpack_from("<H", data, lh + 6)[0]
    struct.pack_into("<H", data, lh + 6, flags | 0x0001)
    ch = find_central_header(bytes(data), name)
    cflags = struct.unpack_from("<H", data, ch + 8)[0]
    struct.pack_into("<H", data, ch + 8, cflags | 0x0001)
    return bytes(data)


def zip_unsupported_method(name: str = "a.txt") -> bytes:
    """Zip claiming compression method 9 (deflate64) — outside the subset."""
    data = bytearray(zip_bytes([{"name": name, "data": b"abc"}]))
    lh = find_local_header(bytes(data), name)
    struct.pack_into("<H", data, lh + 8, 9)
    ch = find_central_header(bytes(data), name)
    struct.pack_into("<H", data, ch + 10, 9)
    return bytes(data)


# ---------------------------------------------------------------------------
# TAR helpers
# ---------------------------------------------------------------------------
def tar_bytes(members: list[dict]) -> bytes:
    """Build a plain tar from member descriptors.

    Keys:
      {name, data}            regular file
      {name, is_dir: True}    directory
      {name, link: target}    symbolic link
      {name, hardlink: tgt}   hard link
      {name, fifo: True}      fifo special file
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for m in members:
            name = m["name"]
            info = tarfile.TarInfo(name)
            if m.get("is_dir"):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tf.addfile(info)
            elif "link" in m:
                info.type = tarfile.SYMTYPE
                info.mode = 0o777
                info.linkname = m["link"]
                tf.addfile(info)
            elif "hardlink" in m:
                info.type = tarfile.LNKTYPE
                info.mode = 0o644
                info.linkname = m["hardlink"]
                tf.addfile(info)
            elif m.get("fifo"):
                info.type = tarfile.FIFOTYPE
                info.mode = 0o600
                tf.addfile(info)
            else:
                payload = m.get("data", b"")
                info.size = len(payload)
                info.mode = m.get("mode", 0o644)
                tf.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def tar_with_truncated_payload(name: str = "a.txt", payload: bytes = b"x" * 2048) -> bytes:
    """Valid headers but payload cut off mid-file -> declared length mismatch."""
    data = tar_bytes([{"name": name, "data": payload}])
    # Cut inside the first 512-byte payload block (after the 512-byte header).
    return data[: 512 + 100]


def _raw_tar_header(
    name: str,
    size: int,
    *,
    typeflag: bytes = b"0",
    mode: int = 0o644,
    linkname: str = "",
) -> bytes:
    """Hand-built 512-byte ustar header (independent of tarfile's writer)."""
    hdr = bytearray(512)

    def put(offset: int, field: bytes, length: int | None = None) -> None:
        hdr[offset : offset + len(field)] = field

    put(0, name.encode("utf-8")[:100])
    put(100, f"{mode:07o}\0".encode())
    put(108, b"0000000\0")  # uid
    put(116, b"0000000\0")  # gid
    put(124, f"{size:011o}\0".encode())
    put(136, b"00000000000\0")  # mtime
    hdr[148:156] = b"        "  # checksum placeholder: eight spaces
    hdr[156] = ord(typeflag)
    put(157, linkname.encode("utf-8")[:100])
    put(257, b"ustar\0" + b"00")
    chk = sum(hdr)
    put(148, f"{chk:06o}\0 ".encode())
    return bytes(hdr)


def tar_with_declared_size_mismatch(name: str = "a.txt") -> bytes:
    """Header claims a big size but the stream ends after a tiny payload.

    The archive has a valid first header, so getmembers() works, but reading the
    payload hits the declared length prematurely -> declared_length_mismatch.
    """
    declared = 8192
    real = b"abc"
    out = bytearray()
    out += _raw_tar_header(name, declared, typeflag=b"0", mode=0o644)
    # Pad the real payload to one 512-byte block then stop (rest missing).
    out += real + b"\0" * (512 - len(real))
    # No end-of-archive zero blocks: forces a length problem, not a clean EOF.
    return bytes(out)


def gzip_of(data: bytes) -> bytes:
    import gzip

    return gzip.compress(data)


def random_bytes(n: int, seed: int = 7) -> bytes:
    # Deterministic, independent of the kernel: use os.urandom with fixed seed
    # via a simple LCG so fixture output is reproducible across machines.
    state = seed
    out = bytearray(n)
    for i in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        out[i] = (state >> 16) & 0xFF
    return bytes(out)
