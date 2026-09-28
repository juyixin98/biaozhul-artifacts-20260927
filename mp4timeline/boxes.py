"""Restricted MP4 (ISO BMFF) box parser.

Scope ("restricted non-encrypted MP4"):
  * plain (non-fragmented) files only — any ``moof``/``mvex``/``mfra`` box is
    rejected with :class:`UnsupportedLayoutError`;
  * unencrypted media only — ``encv``/``enca`` sample entries or a ``tenc``
    box are rejected with :class:`UnsupportedEncryptionError`;
  * every box length is validated against its parent bounds; an over-running
    box raises :class:`BoxOutOfBoundsError`.

The parser extracts only what the timeline kernel needs: movie/track
timescales, the sample tables (stts/ctts/stsc/stsz/stco|co64/stss) and the
edit list (elst).  Tables are returned as NumPy arrays in native byte order.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

from .errors import (
    BoxOutOfBoundsError,
    MalformedBoxError,
    MissingBoxError,
    UnsupportedEncryptionError,
    UnsupportedLayoutError,
)

# Boxes we descend into.  Everything else at the same level is skipped.
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}

# Fragmented-layout markers: presence anywhere we scan => reject.
_FRAGMENTED = {b"moof", b"mvex", b"mfra"}

_ENCRYPTED_ENTRIES = {b"encv", b"enca", b"enct", b"encs", b"encr"}


@dataclass(frozen=True)
class Box:
    type: bytes
    start: int          # offset of the box header in the file
    size: int           # total size including header
    header: int         # header size (8 or 16)

    @property
    def payload_start(self) -> int:
        return self.start + self.header

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass
class EditEntry:
    segment_duration: int | "Fraction"  # movie timescale units (Fraction for implicit edits)
    media_time: int         # in media timescale units; -1 == empty edit
    rate_integer: int
    rate_fraction: int


@dataclass
class TrackBoxes:
    track_id: int
    handler: str
    media_timescale: int
    media_duration: int
    stts_counts: np.ndarray
    stts_deltas: np.ndarray
    ctts_counts: np.ndarray | None      # None => no composition offsets
    ctts_offsets: np.ndarray | None     # signed (ctts v1 keeps sign)
    stsc_first_chunk: np.ndarray
    stsc_samples_per_chunk: np.ndarray
    stsz_sample_size: int
    stsz_sizes: np.ndarray              # per-sample sizes (expanded)
    chunk_offsets: np.ndarray
    stss: np.ndarray | None             # sync sample numbers (1-based) or None
    edits: list[EditEntry] = field(default_factory=list)


@dataclass
class MovieBoxes:
    movie_timescale: int
    movie_duration: int
    brands: list[str]
    tracks: list[TrackBoxes]
    file_size: int


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from(">I", data, off)[0]


def iter_boxes(data: bytes, start: int, end: int):
    """Yield Box headers in [start, end), validating every declared length."""
    pos = start
    while pos < end:
        if end - pos < 8:
            raise MalformedBoxError(
                f"trailing {end - pos} byte(s) at offset {pos} too small for a box header",
                offset=pos,
            )
        size, typ = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if end - pos < 16:
                raise MalformedBoxError("truncated largesize header", box=typ.decode("latin1"), offset=pos)
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos  # box extends to end of parent
        if size < header:
            raise MalformedBoxError(
                f"box size {size} smaller than header {header}",
                box=typ.decode("latin1"), offset=pos,
            )
        if pos + size > end:
            raise BoxOutOfBoundsError(
                f"box declares size {size} at offset {pos}, exceeds parent end {end}",
                box=typ.decode("latin1"), offset=pos,
            )
        yield Box(typ, pos, size, header)
        pos += size


def _fullbox(data: bytes, box: Box) -> tuple[int, int]:
    """Return (version, flags) and validate the fullbox header fits."""
    if box.size - box.header < 4:
        raise MalformedBoxError("fullbox header truncated", box=box.type.decode("latin1"), offset=box.start)
    version = data[box.payload_start]
    flags = int.from_bytes(data[box.payload_start + 1: box.payload_start + 4], "big")
    return version, flags


def _table(data: bytes, box: Box, entry_size: int, dtype) -> np.ndarray:
    """Read a fullbox table of fixed-size entries as a 2-D NumPy array."""
    _fullbox(data, box)
    count = _u32(data, box.payload_start + 4)
    body = box.payload_start + 8
    if body + count * entry_size > box.end:
        raise MalformedBoxError(
            f"table declares {count} entries of {entry_size} bytes but box holds "
            f"{box.end - body} bytes of entries",
            box=box.type.decode("latin1"), offset=box.start,
        )
    arr = np.frombuffer(data, dtype=dtype, count=count * (entry_size // 4), offset=body)
    return arr.reshape(count, entry_size // 4).astype(np.int64)


def _parse_stts(data: bytes, box: Box) -> tuple[np.ndarray, np.ndarray]:
    t = _table(data, box, 8, ">u4")
    return t[:, 0], t[:, 1]


def _parse_ctts(data: bytes, box: Box) -> tuple[np.ndarray, np.ndarray]:
    version, _ = _fullbox(data, box)
    count = _u32(data, box.payload_start + 4)
    body = box.payload_start + 8
    if body + count * 8 > box.end:
        raise MalformedBoxError("ctts table truncated", box="ctts", offset=box.start)
    counts = np.frombuffer(data, dtype=">u4", count=count * 2, offset=body)[0::2].astype(np.int64)
    # version 0: unsigned offsets; version 1: signed (sign preserved)
    dt = ">i4" if version == 1 else ">u4"
    offsets = np.frombuffer(data, dtype=dt, count=count * 2, offset=body)[1::2].astype(np.int64)
    return counts, offsets


def _parse_stsc(data: bytes, box: Box) -> tuple[np.ndarray, np.ndarray]:
    t = _table(data, box, 12, ">u4")
    return t[:, 0], t[:, 1]


def _parse_stsz(data: bytes, box: Box) -> tuple[int, np.ndarray]:
    _fullbox(data, box)
    sample_size = _u32(data, box.payload_start + 4)
    count = _u32(data, box.payload_start + 8)
    if sample_size != 0:
        return sample_size, np.full(count, sample_size, dtype=np.int64)
    body = box.payload_start + 12
    if body + count * 4 > box.end:
        raise MalformedBoxError("stsz table truncated", box="stsz", offset=box.start)
    sizes = np.frombuffer(data, dtype=">u4", count=count, offset=body).astype(np.int64)
    return 0, sizes


def _parse_stco(data: bytes, box: Box, wide: bool) -> np.ndarray:
    _fullbox(data, box)
    count = _u32(data, box.payload_start + 4)
    body = box.payload_start + 8
    esize = 8 if wide else 4
    if body + count * esize > box.end:
        raise MalformedBoxError("chunk offset table truncated", box=box.type.decode("latin1"), offset=box.start)
    dt = ">u8" if wide else ">u4"
    return np.frombuffer(data, dtype=dt, count=count, offset=body).astype(np.int64)


def _parse_stss(data: bytes, box: Box) -> np.ndarray:
    _fullbox(data, box)
    count = _u32(data, box.payload_start + 4)
    body = box.payload_start + 8
    if body + count * 4 > box.end:
        raise MalformedBoxError("stss table truncated", box="stss", offset=box.start)
    return np.frombuffer(data, dtype=">u4", count=count, offset=body).astype(np.int64)


def _parse_elst(data: bytes, box: Box) -> list[EditEntry]:
    version, _ = _fullbox(data, box)
    count = _u32(data, box.payload_start + 4)
    body = box.payload_start + 8
    esize = 20 if version == 1 else 12
    if body + count * esize > box.end:
        raise MalformedBoxError("elst table truncated", box="elst", offset=box.start)
    edits: list[EditEntry] = []
    for i in range(count):
        off = body + i * esize
        if version == 1:
            seg, media_time = struct.unpack_from(">Qq", data, off)
            rate_int, rate_frac = struct.unpack_from(">hH", data, off + 16)
        else:
            seg, media_time = struct.unpack_from(">Ii", data, off)
            rate_int, rate_frac = struct.unpack_from(">hH", data, off + 8)
        edits.append(EditEntry(int(seg), int(media_time), int(rate_int), int(rate_frac)))
    return edits


def _parse_mdhd(data: bytes, box: Box) -> tuple[int, int]:
    version, _ = _fullbox(data, box)
    p = box.payload_start + 4
    if version == 1:
        timescale = _u32(data, p + 16)
        duration = struct.unpack_from(">Q", data, p + 20)[0]
    else:
        timescale = _u32(data, p + 8)
        duration = _u32(data, p + 12)
    if timescale == 0:
        raise MalformedBoxError("mdhd timescale is zero", box="mdhd", offset=box.start)
    return timescale, int(duration)


def _parse_mvhd(data: bytes, box: Box) -> tuple[int, int]:
    version, _ = _fullbox(data, box)
    p = box.payload_start + 4
    if version == 1:
        timescale = _u32(data, p + 16)
        duration = struct.unpack_from(">Q", data, p + 20)[0]
    else:
        timescale = _u32(data, p + 8)
        duration = _u32(data, p + 12)
    if timescale == 0:
        raise MalformedBoxError("mvhd timescale is zero", box="mvhd", offset=box.start)
    return timescale, int(duration)


def _parse_tkhd(data: bytes, box: Box) -> int:
    version, _ = _fullbox(data, box)
    p = box.payload_start + 4
    if version == 1:
        return _u32(data, p + 16)
    return _u32(data, p + 8)


def _parse_hdlr(data: bytes, box: Box) -> str:
    _fullbox(data, box)
    if box.payload_start + 12 > box.end:
        raise MalformedBoxError("hdlr truncated", box="hdlr", offset=box.start)
    return data[box.payload_start + 8: box.payload_start + 12].decode("latin1")


def _check_stsd(data: bytes, box: Box) -> None:
    """Reject encrypted sample entries; we do not decode stsd otherwise."""
    _fullbox(data, box)
    count = _u32(data, box.payload_start + 4)
    pos = box.payload_start + 8
    for _ in range(count):
        if pos + 8 > box.end:
            raise MalformedBoxError("stsd entry truncated", box="stsd", offset=box.start)
        size, typ = struct.unpack_from(">I4s", data, pos)
        if size < 8 or pos + size > box.end:
            raise MalformedBoxError("stsd entry size invalid", box="stsd", offset=pos)
        if typ in _ENCRYPTED_ENTRIES:
            raise UnsupportedEncryptionError(
                f"encrypted sample entry {typ.decode('latin1')!r} is not supported",
                box="stsd", offset=pos,
            )
        payload = data[pos + 8: pos + size]
        if b"tenc" in payload or b"sinf" in payload:
            raise UnsupportedEncryptionError(
                "sample entry carries tenc/sinf (encrypted media)", box="stsd", offset=pos,
            )
        pos += size


class _TrackBuilder:
    def __init__(self) -> None:
        self.kw: dict = {}
        self.edits: list[EditEntry] = []

    def handle(self, data: bytes, box: Box) -> None:
        t = box.type
        if t == b"tkhd":
            self.kw["track_id"] = _parse_tkhd(data, box)
        elif t == b"mdhd":
            ts, dur = _parse_mdhd(data, box)
            self.kw["media_timescale"], self.kw["media_duration"] = ts, dur
        elif t == b"hdlr":
            self.kw["handler"] = _parse_hdlr(data, box)
        elif t == b"stts":
            self.kw["stts_counts"], self.kw["stts_deltas"] = _parse_stts(data, box)
        elif t == b"ctts":
            self.kw["ctts_counts"], self.kw["ctts_offsets"] = _parse_ctts(data, box)
        elif t == b"stsc":
            self.kw["stsc_first_chunk"], self.kw["stsc_samples_per_chunk"] = _parse_stsc(data, box)
        elif t == b"stsz":
            self.kw["stsz_sample_size"], self.kw["stsz_sizes"] = _parse_stsz(data, box)
        elif t == b"stco":
            self.kw["chunk_offsets"] = _parse_stco(data, box, wide=False)
        elif t == b"co64":
            self.kw["chunk_offsets"] = _parse_stco(data, box, wide=True)
        elif t == b"stss":
            self.kw["stss"] = _parse_stss(data, box)
        elif t == b"elst":
            self.edits = _parse_elst(data, box)
        elif t == b"stsd":
            _check_stsd(data, box)

    def build(self) -> TrackBoxes:
        required = [
            "track_id", "handler", "media_timescale", "media_duration",
            "stts_counts", "stsc_first_chunk", "stsz_sizes", "chunk_offsets",
        ]
        missing = [k for k in required if k not in self.kw]
        if missing:
            raise MissingBoxError(f"track missing required tables: {', '.join(missing)}")
        return TrackBoxes(
            ctts_counts=self.kw.get("ctts_counts"),
            ctts_offsets=self.kw.get("ctts_offsets"),
            stss=self.kw.get("stss"),
            edits=self.edits,
            **{k: v for k, v in self.kw.items() if k not in ("ctts_counts", "ctts_offsets", "stss")},
        )


def _walk(data: bytes, start: int, end: int, movie: dict, track: _TrackBuilder | None, depth: int) -> None:
    for box in iter_boxes(data, start, end):
        t = box.type
        if t in _FRAGMENTED:
            raise UnsupportedLayoutError(
                f"fragmented MP4 marker {t.decode('latin1')!r} found; only plain "
                "non-fragmented files are supported",
                box=t.decode("latin1"), offset=box.start,
            )
        if t == b"mvhd":
            movie["movie_timescale"], movie["movie_duration"] = _parse_mvhd(data, box)
        elif t == b"trak":
            child = _TrackBuilder()
            _walk(data, box.payload_start, box.end, movie, child, depth + 1)
            movie["tracks"].append(child.build())
        elif t in _CONTAINERS:
            _walk(data, box.payload_start, box.end, movie, track, depth + 1)
        elif track is not None:
            track.handle(data, box)


def parse_movie(data: bytes) -> MovieBoxes:
    """Parse a whole in-memory MP4 file into a :class:`MovieBoxes`."""
    movie: dict = {"tracks": []}
    brands: list[str] = []
    saw_moov = False
    for box in iter_boxes(data, 0, len(data)):
        if box.type in _FRAGMENTED:
            raise UnsupportedLayoutError(
                f"fragmented MP4 marker {box.type.decode('latin1')!r} found; only plain "
                "non-fragmented files are supported",
                box=box.type.decode("latin1"), offset=box.start,
            )
        if box.type == b"ftyp":
            p = box.payload_start
            if p + 8 > box.end:
                raise MalformedBoxError("ftyp truncated", box="ftyp", offset=box.start)
            brands = [data[i: i + 4].decode("latin1") for i in range(p, box.end, 4)]
        elif box.type == b"moov":
            saw_moov = True
            _walk(data, box.payload_start, box.end, movie, None, 1)
    if not saw_moov:
        raise MissingBoxError("no moov box found")
    if "movie_timescale" not in movie:
        raise MissingBoxError("moov contains no mvhd box")
    if not movie["tracks"]:
        raise MissingBoxError("moov contains no trak boxes")
    return MovieBoxes(
        movie_timescale=movie["movie_timescale"],
        movie_duration=movie["movie_duration"],
        brands=brands,
        tracks=movie["tracks"],
        file_size=len(data),
    )
