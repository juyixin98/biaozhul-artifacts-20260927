"""Media parsing: raw PCM and uncompressed PCM/float WAV -> normalized float.

Only local, uncompressed, off-line input is supported. Output is float64 PCM
approximately in [-1.0, 1.0], shaped (samples, channels).

Channel layouts (WAVE channel ordering, matching ffmpeg's aresample matrix):

  mono   [C]                                   -> keep [C]
  stereo [L, R]                                -> keep [L, R]
  5.0    [L, R, C, Ls, Rs]                     -> keep all,  G = [1,1,1,1.41,1.41]
  5.1    [L, R, C, LFE, Ls, Rs]                -> drop LFE,  G = [1,1,1,1.41,1.41]

The LFE channel is discarded: BS.1770/R128 loudness does not include LFE.
Compressed formats (MP3/AAC/Opus/...) are deliberately rejected.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

# Analysis channel order is always [L, R, C, Ls, Rs] when present.
LAYOUT_MONO = "mono"
LAYOUT_STEREO = "stereo"
LAYOUT_50 = "5.0"
LAYOUT_51 = "5.1"

#: source channel index -> (analysis position, BS.1770 gain)
_LAYOUT_SELECTORS: dict[str, list[tuple[int, float]]] = {
    LAYOUT_MONO: [(0, 1.0)],
    LAYOUT_STEREO: [(0, 1.0), (1, 1.0)],
    LAYOUT_50: [(0, 1.0), (1, 1.0), (2, 1.0), (3, 1.41), (4, 1.41)],
    LAYOUT_51: [(0, 1.0), (1, 1.0), (2, 1.0), (4, 1.41), (5, 1.41)],
}

WAVE_TAG_PCM = 0x0001
WAVE_TAG_FLOAT = 0x0003
WAVE_TAG_EXTENSIBLE = 0xFFFE

# Known WAVE_FORMAT_EXTENSIBLE subformat GUID tails (first two bytes = tag).
_KSDATAFORMAT_SUBTYPE_PCM = bytes.fromhex("000000000010000080000000aa00389b")
_KSDATAFORMAT_SUBTYPE_IEEE_FLOAT = bytes.fromhex("010000000000100080000000aa00389b")

PCM_FORMAT_S16 = "s16"
PCM_FORMAT_S24 = "s24"
PCM_FORMAT_S32 = "s32"
PCM_FORMAT_F32 = "f32"
RAW_PCM_FORMATS = (PCM_FORMAT_S16, PCM_FORMAT_S24, PCM_FORMAT_S32, PCM_FORMAT_F32)


class MediaError(ValueError):
    """Parsing error carrying a stable machine-readable failure category."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DecodedAudio:
    samples: np.ndarray            # shape (n, num_analysis_channels), float64
    sample_rate: int
    layout: str
    channel_weights: tuple[float, ...]
    source_channels: int
    source_format: str


def decode_raw_pcm(payload: bytes, *, sample_rate: int, channels: int,
                   sample_format: str) -> DecodedAudio:
    """Decode headerless little-endian PCM with an explicit descriptor."""
    layout = _layout_for_channels(channels)
    frame_bytes = _frame_size_bytes(sample_format, channels)
    if len(payload) % frame_bytes != 0:
        raise MediaError(
            "PCM_TRUNCATED",
            f"payload {len(payload)} bytes is not a multiple of frame size "
            f"{frame_bytes} ({channels} ch * {sample_format})")
    interleaved = _pcm_to_float(payload, sample_format)
    return _build_decoded(interleaved, sample_rate, layout, sample_format)


def _frame_size_bytes(sample_format: str, channels: int) -> int:
    per_sample = {"u8": 1, PCM_FORMAT_S16: 2, PCM_FORMAT_S24: 3,
                  PCM_FORMAT_S32: 4, PCM_FORMAT_F32: 4}[sample_format]
    return per_sample * channels


def decode_wav(payload: bytes, *, layout_override: str | None = None) -> DecodedAudio:
    """Decode an uncompressed PCM/IEEE-float WAV from its bytes.

    Parsed manually (rather than via ``wave``) so that 24-bit PCM and float WAV
    are both supported with explicit failure categories.
    """
    if len(payload) < 44:
        raise MediaError("WAV_TOO_SHORT", "payload smaller than a minimal WAV header")
    if payload[0:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise MediaError("WAV_NOT_RIFF", "not a RIFF/WAVE container")
    if payload[0:4] == b"RIFX":
        raise MediaError("WAV_BIG_ENDIAN_UNSUPPORTED", "RIFX big-endian WAV is not supported")

    fmt_chunk = _find_riff_chunk(payload, b"fmt ")
    if fmt_chunk is None:
        raise MediaError("WAV_MISSING_FMT", "WAV has no fmt chunk")
    fmt_offset, fmt_size, _ = fmt_chunk
    data_chunk = _find_riff_chunk(payload, b"data")
    if data_chunk is None:
        raise MediaError("WAV_MISSING_DATA", "WAV has no data chunk")
    data_offset, data_size, _chunk_end = data_chunk
    fmt = payload[fmt_offset:fmt_offset + fmt_size]

    if len(fmt) < 16:
        raise MediaError("WAV_MALFORMED_FMT", "fmt chunk shorter than 16 bytes")
    (audio_format, channels, sample_rate, _byte_rate, _block_align,
     bits_per_sample) = struct.unpack_from("<HHIIHH", fmt, 0)
    if audio_format == WAVE_TAG_EXTENSIBLE:
        if len(fmt) < 40:
            raise MediaError("WAV_MALFORMED_FMT", "extensible fmt chunk truncated")
        audio_format = struct.unpack_from("<H", fmt, 24)[0]
        guid_tail = bytes(fmt[26:42])
        if guid_tail not in (_KSDATAFORMAT_SUBTYPE_PCM, _KSDATAFORMAT_SUBTYPE_IEEE_FLOAT):
            raise MediaError(
                "WAV_COMPRESSED_UNSUPPORTED",
                "only PCM and IEEE float WAVE_FORMAT_EXTENSIBLE subformats are supported")

    if audio_format == WAVE_TAG_PCM:
        sample_format = {8: "u8", 16: PCM_FORMAT_S16,
                         24: PCM_FORMAT_S24, 32: PCM_FORMAT_S32}.get(bits_per_sample)
        if sample_format is None:
            raise MediaError(
                "WAV_UNSUPPORTED_BIT_DEPTH",
                f"PCM bit depth {bits_per_sample} is not supported (8/16/24/32)")
    elif audio_format == WAVE_TAG_FLOAT:
        if bits_per_sample != 32:
            raise MediaError(
                "WAV_UNSUPPORTED_BIT_DEPTH",
                f"float bit depth {bits_per_sample} is not supported (f32 only)")
        sample_format = PCM_FORMAT_F32
    else:
        raise MediaError(
            "WAV_COMPRESSED_UNSUPPORTED",
            f"wave format tag 0x{audio_format:04x} is compressed or unsupported")

    raw = payload[data_offset:data_offset + data_size]
    frame_bytes = _frame_size_bytes(sample_format, channels)
    if len(raw) % frame_bytes != 0:
        raise MediaError(
            "WAV_TRUNCATED_DATA",
            f"data chunk ({len(raw)} bytes) is not a multiple of frame size "
            f"{frame_bytes} ({channels} ch * {sample_format})")
    interleaved = _pcm_to_float(raw, sample_format)
    layout = layout_override or _layout_for_channels(channels)
    if layout_override is not None:
        # An override must still be compatible with the physical channel count.
        expected_channels = {
            LAYOUT_MONO: 1, LAYOUT_STEREO: 2, LAYOUT_50: 5, LAYOUT_51: 6}[layout_override]
        if expected_channels != channels:
            raise MediaError(
                "LAYOUT_CHANNEL_MISMATCH",
                f"declared layout {layout_override} needs {expected_channels} channels, "
                f"file has {channels}")
    decoded = _build_decoded(interleaved, sample_rate, layout, f"wav:{sample_format}")
    return decoded


def _build_decoded(interleaved: np.ndarray, sample_rate: int, layout: str,
                   source_format: str) -> DecodedAudio:
    selector = _LAYOUT_SELECTORS[layout]
    source_channels = 6 if layout == LAYOUT_51 else len(selector)
    n_total = interleaved.shape[0]
    if n_total % source_channels != 0:
        raise MediaError(
            "PCM_TRUNCATED",
            f"sample count {n_total} is not divisible by {source_channels} channels")
    frames = interleaved.reshape(-1, source_channels)
    indices = [src for src, _gain in selector]
    weights = tuple(gain for _src, gain in selector)
    samples = frames[:, indices].astype(np.float64, copy=True)
    return DecodedAudio(
        samples=samples,
        sample_rate=int(sample_rate),
        layout=layout,
        channel_weights=weights,
        source_channels=source_channels,
        source_format=source_format,
    )


def _layout_for_channels(channels: int) -> str:
    for layout, selector in _LAYOUT_SELECTORS.items():
        physical = 6 if layout == LAYOUT_51 else len(selector)
        if physical == channels:
            return layout
    raise MediaError(
        "UNSUPPORTED_CHANNEL_LAYOUT",
        f"{channels} channels is not supported (mono/stereo/5.0/5.1 only)")


def _pcm_to_float(payload: bytes, sample_format: str) -> np.ndarray:
    if sample_format == "u8":
        return (np.frombuffer(payload, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    if sample_format == PCM_FORMAT_S16:
        return np.frombuffer(payload, dtype="<i2").astype(np.float64) / 32768.0
    if sample_format == PCM_FORMAT_S32:
        return np.frombuffer(payload, dtype="<i4").astype(np.float64) / 2147483648.0
    if sample_format == PCM_FORMAT_F32:
        return np.frombuffer(payload, dtype="<f4").astype(np.float64)
    if sample_format == PCM_FORMAT_S24:
        return _s24_to_float(payload)
    raise MediaError("UNSUPPORTED_PCM_FORMAT", f"unknown PCM format {sample_format!r}")


def _s24_to_float(payload: bytes) -> np.ndarray:
    """Decode packed little-endian signed 24-bit PCM (no external libs)."""
    if len(payload) % 3 != 0:
        raise MediaError(
            "PCM_TRUNCATED", f"s24 payload length {len(payload)} is not a multiple of 3")
    raw = np.frombuffer(payload, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    value = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
    # Sign-extend from 24 bits.
    value = np.where(value >= 0x800000, value - 0x1000000, value)
    return value.astype(np.float64) / 8388608.0


def _find_riff_chunk(payload: bytes, chunk_id: bytes) -> tuple[int, int, int] | None:
    """Return (data_offset, data_size, chunk_end) for the first matching chunk."""
    pos = 12
    n = len(payload)
    while pos + 8 <= n:
        cid = payload[pos:pos + 4]
        (size,) = struct.unpack_from("<I", payload, pos + 4)
        data_offset = pos + 8
        chunk_end = data_offset + size
        if chunk_end > n:
            # data chunk may legitimately report size larger than what we hold
            # only if it is the final chunk; clamp in the caller via slicing.
            if cid == b"data":
                return data_offset, min(size, n - data_offset), n
            raise MediaError("WAV_MALFORMED_CHUNK", f"chunk {cid!r} exceeds container")
        if cid == chunk_id:
            return data_offset, size, chunk_end
        pos = chunk_end + (size & 1)  # chunks are word-aligned (pad byte)
    return None
