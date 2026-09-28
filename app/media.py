"""媒体解析：把上传字节解析成单声道 float64 采样（归一化到 [-1, 1]）。

支持矩阵（故意收窄，拒绝即给 ``MEDIA_PARSE_ERROR``，不静默猜测）：

* WAV 容器（``RIFF....WAVE``）：PCM s16（fmt code 1）或 FLOAT f32（code 3），
  仅支持单声道；位深、声道数不符直接拒绝。
* 裸 PCM（``format=raw`` 或字节不像 RIFF）：``s16le``（默认）或 ``f32le``，
  声道布局固定为单声道。字节长度不是位深整倍数时拒绝。

返回 :class:`AudioData`。非有限采样（NaN/Inf，仅 f32 可能出现）在解析阶段
保留原样，由信号内核在计算时归类为 ``COMPUTATION_FAILED``——解析错误
（``MEDIA_PARSE_ERROR``）与计算错误（``COMPUTATION_FAILED``）因此可区分。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from .errors import SegmentError

_S16LE = "s16le"
_F32LE = "f32le"
_RAW_FORMATS = {_S16LE, _F32LE}


@dataclass(frozen=True)
class AudioData:
    """解析结果。

    Attributes:
        samples: float64 一维数组，半开区间 ``[0, len(samples))`` 即原音频域。
        sample_rate: Hz；WAV 取文件头，裸 PCM 取调用方参数。
        source_format: ``wav:s16`` / ``wav:f32`` / ``raw:s16le`` / ``raw:f32le``。
        total_samples: 样本总数。
    """

    samples: np.ndarray
    sample_rate: int
    source_format: str

    @property
    def total_samples(self) -> int:
        return int(self.samples.shape[0])


def _decode_s16(raw: bytes) -> np.ndarray:
    if len(raw) % 2:
        raise SegmentError(
            "MEDIA_PARSE_ERROR",
            "s16 PCM byte length must be a multiple of 2",
            trailing_bytes=len(raw) % 2,
        )
    return np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0


def _decode_f32(raw: bytes) -> np.ndarray:
    if len(raw) % 4:
        raise SegmentError(
            "MEDIA_PARSE_ERROR",
            "f32 PCM byte length must be a multiple of 4",
            trailing_bytes=len(raw) % 4,
        )
    return np.frombuffer(raw, dtype="<f4").astype(np.float64)


def _iter_riff_chunks(body: bytes, offset: int = 12) -> list[tuple[bytes, bytes]]:
    chunks: list[tuple[bytes, bytes]] = []
    pos = offset
    while pos + 8 <= len(body):
        chunk_id = body[pos : pos + 4]
        (size,) = struct.unpack_from("<I", body, pos + 4)
        data_start = pos + 8
        data_end = data_start + size
        if data_end > len(body):
            raise SegmentError(
                "MEDIA_PARSE_ERROR",
                f"RIFF chunk {chunk_id!r} declares {size} bytes past end of file",
                chunk=chunk_id.decode("ascii", "replace"),
            )
        chunks.append((chunk_id, body[data_start:data_end]))
        pos = data_end + (size & 1)  # 块按字对齐，奇数补 1 字节
    return chunks


def _parse_wav(raw: bytes) -> tuple[np.ndarray, int, str]:
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise SegmentError(
            "MEDIA_PARSE_ERROR", "not a RIFF/WAVE file", magic=raw[:4].hex()
        )
    chunks = dict(_iter_riff_chunks(raw))
    if b"fmt " not in chunks:
        raise SegmentError("MEDIA_PARSE_ERROR", "WAV missing 'fmt ' chunk")
    if b"data" not in chunks:
        raise SegmentError("MEDIA_PARSE_ERROR", "WAV missing 'data' chunk")
    fmt = chunks[b"fmt "]
    if len(fmt) < 16:
        raise SegmentError(
            "MEDIA_PARSE_ERROR", "WAV 'fmt ' chunk too short", fmt_bytes=len(fmt)
        )
    format_tag, channels, sample_rate, _byte_rate, _block_align, bits = (
        struct.unpack_from("<HHIIHH", fmt, 0)
    )
    data = chunks[b"data"]

    if channels != 1:
        raise SegmentError(
            "MEDIA_PARSE_ERROR",
            "only mono PCM is supported; downmix policy is intentionally absent",
            channels=channels,
        )
    if format_tag == 1 and bits == 16:
        samples = _decode_s16(data)
        kind = "wav:s16"
    elif format_tag == 3 and bits == 32:
        samples = _decode_f32(data)
        kind = "wav:f32"
    else:
        raise SegmentError(
            "MEDIA_PARSE_ERROR",
            "unsupported WAV format: only mono PCM s16 (tag 1) or FLOAT f32 "
            "(tag 3) are supported",
            format_tag=format_tag,
            bits_per_sample=bits,
        )
    if sample_rate <= 0:
        raise SegmentError(
            "MEDIA_PARSE_ERROR", "WAV header has non-positive sample rate",
            sample_rate=sample_rate,
        )
    return samples, int(sample_rate), kind


def parse_audio(
    raw: bytes,
    *,
    fmt: str = "auto",
    sample_rate: int | None = None,
) -> AudioData:
    """字节 -> :class:`AudioData`。

    Args:
        raw: 上传的原始字节。
        fmt: ``auto``（WAV 魔数优先，否则按裸 s16le）、``wav``、``raw:s16le``、
            ``raw:f32le``。
        sample_rate: 裸 PCM 必填；WAV 若给出则必须与文件头一致（防误配）。
    """
    if not raw:
        raise SegmentError("EMPTY_INPUT", "uploaded media contains 0 bytes")
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise SegmentError("INVALID_ARGUMENT", "media payload must be bytes")

    looks_wav = len(raw) >= 12 and raw[0:4] == b"RIFF" and raw[8:12] == b"WAVE"
    want = fmt.lower().strip()
    if want == "auto":
        want = "wav" if looks_wav else f"raw:{_S16LE}"

    if want == "wav":
        samples, header_rate, kind = _parse_wav(bytes(raw))
        if sample_rate is not None and int(sample_rate) != header_rate:
            raise SegmentError(
                "MEDIA_PARSE_ERROR",
                "sample_rate parameter does not match WAV header",
                provided=int(sample_rate),
                header=header_rate,
            )
        rate = header_rate
    elif want.startswith("raw:") or want in _RAW_FORMATS:
        codec = want.split(":", 1)[1] if ":" in want else want
        if codec not in _RAW_FORMATS:
            raise SegmentError(
                "MEDIA_PARSE_ERROR",
                "raw PCM codec must be s16le or f32le",
                codec=codec,
            )
        if not sample_rate:
            raise SegmentError(
                "INVALID_ARGUMENT",
                "sample_rate is required for raw PCM input",
                field="sample_rate",
            )
        if int(sample_rate) <= 0:
            raise SegmentError(
                "INVALID_ARGUMENT", "sample_rate must be positive",
                field="sample_rate",
            )
        samples = _decode_s16(raw) if codec == _S16LE else _decode_f32(raw)
        kind = f"raw:{codec}"
        rate = int(sample_rate)
    else:
        raise SegmentError(
            "INVALID_ARGUMENT",
            "fmt must be one of auto/wav/raw:s16le/raw:f32le",
            fmt=fmt,
        )

    if samples.size == 0:
        raise SegmentError("EMPTY_INPUT", "media decoded to 0 samples", format=kind)
    return AudioData(samples=samples, sample_rate=rate, source_format=kind)
