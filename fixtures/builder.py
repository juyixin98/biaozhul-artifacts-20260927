"""Synthetic MP4 writer used to build test fixtures.

This is the *writer* side; it shares no code with the parser under test
(``mp4timeline.boxes``), so fixture bytes and parser logic are independent.
Layout is always ``ftyp | mdat | moov`` so chunk offsets are known before the
moov is serialized.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field


def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def fullbox(typ: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(typ, bytes([version]) + flags.to_bytes(3, "big") + payload)


def ftyp(major: bytes = b"isom", minor: int = 0x200, brands: tuple[bytes, ...] = (b"isom", b"iso2")) -> bytes:
    return box(b"ftyp", major + struct.pack(">I", minor) + b"".join(brands))


def mvhd(timescale: int, duration: int) -> bytes:
    payload = (
        struct.pack(">IIII", 0, 0, timescale, duration)
        + struct.pack(">I", 0x00010000)      # rate 1.0
        + struct.pack(">H", 0x0100)          # volume 1.0
        + b"\x00" * 10
        + struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        + b"\x00" * 24
        + struct.pack(">I", 3)               # next_track_id
    )
    return fullbox(b"mvhd", 0, 0, payload)


def tkhd(track_id: int, duration: int, width: int = 0, height: int = 0) -> bytes:
    payload = (
        struct.pack(">IIIII", 0, 0, track_id, 0, duration)
        + b"\x00" * 8
        + struct.pack(">hhhh", 0, 0, 0, 0)
        + struct.pack(">9I", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
        + struct.pack(">II", width << 16, height << 16)
    )
    return fullbox(b"tkhd", 0, 0x000007, payload)


def mdhd(timescale: int, duration: int) -> bytes:
    payload = struct.pack(">IIII", 0, 0, timescale, duration) + struct.pack(">HH", 0x55C4, 0)
    return fullbox(b"mdhd", 0, 0, payload)


def hdlr(handler: bytes) -> bytes:
    name = {b"vide": b"VideoHandler\x00", b"soun": b"SoundHandler\x00"}.get(handler, b"H\x00")
    return fullbox(b"hdlr", 0, 0, b"\x00" * 4 + handler + b"\x00" * 12 + name)


def elst(entries: list[tuple[int, int, int, int]], version: int = 0) -> bytes:
    """entries: (segment_duration, media_time, rate_integer, rate_fraction)."""
    payload = struct.pack(">I", len(entries))
    for seg, media_time, ri, rf in entries:
        if version == 1:
            payload += struct.pack(">Qq", seg, media_time) + struct.pack(">hH", ri, rf)
        else:
            payload += struct.pack(">Ii", seg, media_time) + struct.pack(">hH", ri, rf)
    return fullbox(b"elst", version, 0, payload)


def stsd(entry_type: bytes) -> bytes:
    # Minimal sample entry: 6 reserved bytes + data_reference_index, padded.
    entry = box(entry_type, b"\x00" * 6 + struct.pack(">H", 1) + b"\x00" * 70)
    return fullbox(b"stsd", 0, 0, struct.pack(">I", 1) + entry)


def stts(entries: list[tuple[int, int]]) -> bytes:
    payload = struct.pack(">I", len(entries))
    for count, delta in entries:
        payload += struct.pack(">II", count, delta)
    return fullbox(b"stts", 0, 0, payload)


def ctts(entries: list[tuple[int, int]], version: int = 0) -> bytes:
    payload = struct.pack(">I", len(entries))
    for count, offset in entries:
        if version == 1:
            payload += struct.pack(">Ii", count, offset)
        else:
            payload += struct.pack(">II", count, offset)
    return fullbox(b"ctts", version, 0, payload)


def stsc(entries: list[tuple[int, int, int]]) -> bytes:
    payload = struct.pack(">I", len(entries))
    for first_chunk, spc, desc in entries:
        payload += struct.pack(">III", first_chunk, spc, desc)
    return fullbox(b"stsc", 0, 0, payload)


def stsz(sizes: list[int]) -> bytes:
    if len(set(sizes)) == 1:
        return fullbox(b"stsz", 0, 0, struct.pack(">II", sizes[0], len(sizes)))
    payload = struct.pack(">II", 0, len(sizes)) + b"".join(struct.pack(">I", s) for s in sizes)
    return fullbox(b"stsz", 0, 0, payload)


def stco(offsets: list[int]) -> bytes:
    payload = struct.pack(">I", len(offsets)) + b"".join(struct.pack(">I", o) for o in offsets)
    return fullbox(b"stco", 0, 0, payload)


def co64(offsets: list[int]) -> bytes:
    payload = struct.pack(">I", len(offsets)) + b"".join(struct.pack(">Q", o) for o in offsets)
    return fullbox(b"co64", 0, 0, payload)


def stss(samples: list[int]) -> bytes:
    payload = struct.pack(">I", len(samples)) + b"".join(struct.pack(">I", s) for s in samples)
    return fullbox(b"stss", 0, 0, payload)


@dataclass
class TrackSpec:
    track_id: int
    handler: bytes                 # b"vide" | b"soun"
    media_timescale: int
    media_duration: int
    sample_sizes: list[int]
    stts_entries: list[tuple[int, int]]
    chunk_offsets: list[int] = field(default_factory=list)  # filled by make_file
    stsc_entries: list[tuple[int, int, int]] = ((1, 0, 1),)  # spc patched below
    ctts_entries: list[tuple[int, int]] | None = None
    ctts_version: int = 0
    stss_samples: list[int] | None = None
    elst_entries: list[tuple[int, int, int, int]] | None = None
    elst_version: int = 0
    stsd_entry: bytes = b"avc1"
    use_co64: bool = False

    def __post_init__(self):
        # default stsc: one run, all samples in a single chunk
        if self.stsc_entries == ((1, 0, 1),):
            self.stsc_entries = [(1, len(self.sample_sizes), 1)]


def _trak(spec: TrackSpec) -> bytes:
    stbl_children = [
        stsd(spec.stsd_entry),
        stts(spec.stts_entries),
        stsc(spec.stsc_entries),
        stsz(spec.sample_sizes),
        co64(spec.chunk_offsets) if spec.use_co64 else stco(spec.chunk_offsets),
    ]
    if spec.ctts_entries is not None:
        stbl_children.append(ctts(spec.ctts_entries, version=spec.ctts_version))
    if spec.stss_samples is not None:
        stbl_children.append(stss(spec.stss_samples))
    stbl = box(b"stbl", b"".join(stbl_children))

    if spec.handler == b"vide":
        media_header = fullbox(b"vmhd", 0, 1, b"\x00" * 8)
    else:
        media_header = fullbox(b"smhd", 0, 0, b"\x00" * 4)
    dref = fullbox(b"dref", 0, 0, struct.pack(">I", 1) + fullbox(b"url ", 0, 1, b""))
    minf = box(b"minf", media_header + box(b"dinf", dref) + stbl)
    mdia = box(b"mdia", mdhd(spec.media_timescale, spec.media_duration) + hdlr(spec.handler) + minf)

    children = tkhd(spec.track_id, spec.media_duration)
    if spec.elst_entries is not None:
        children += box(b"edts", elst(spec.elst_entries, version=spec.elst_version))
    children += mdia
    return box(b"trak", children)


def chunk_sample_counts(stsc_entries: list[tuple[int, int, int]], n_samples: int) -> list[int]:
    """Expand stsc runs into a per-chunk sample count (writer-side mirror of
    the ISO rule; kept here so the fixture layout is explicit)."""
    counts: list[int] = []
    remaining = n_samples
    for i, (_first, spc, _desc) in enumerate(stsc_entries):
        if i + 1 < len(stsc_entries):
            n_chunks = stsc_entries[i + 1][0] - stsc_entries[i][0]
        else:
            n_chunks = -(-remaining // spc)  # last run covers what is left
        for _ in range(n_chunks):
            take = min(spc, remaining)
            counts.append(take)
            remaining -= take
    if remaining != 0:
        raise ValueError(f"stsc layout covers {n_samples - remaining} of {n_samples} samples")
    return counts


def make_file(tracks: list[TrackSpec], movie_timescale: int, movie_duration: int) -> bytes:
    """Serialize ftyp|mdat|moov.  Sample payload bytes are deterministic
    (sample i of track t filled with byte value t*16+i) so byte ranges can be
    verified by content, not just offsets."""
    head = ftyp()
    mdat_payload = b""
    cursor = len(head) + 8  # mdat header
    for t_idx, spec in enumerate(tracks):
        offsets = []
        pos = 0
        for count in chunk_sample_counts(spec.stsc_entries, len(spec.sample_sizes)):
            offsets.append(cursor)
            cursor += sum(spec.sample_sizes[pos: pos + count])
            pos += count
        spec.chunk_offsets = offsets
        for i, size in enumerate(spec.sample_sizes):
            mdat_payload += bytes([(t_idx * 16 + i) & 0xFF]) * size
    moov = box(
        b"moov",
        mvhd(movie_timescale, movie_duration) + b"".join(_trak(t) for t in tracks),
    )
    return head + box(b"mdat", mdat_payload) + moov
