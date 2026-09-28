"""Media boundary: parse mono integer PCM WAV into float64 and back.

Only uncompressed PCM (WAV format tag 1) mono files are accepted.  Supported
bit depths are 16, 24 and 32 bits per sample (the common PCM range).  Values
are scaled to the symmetric convention ``-1.0 .. +1.0`` (the 24-bit path
scales by 2^23, never 2^24, and the unused asymmetric negative extreme is
mapped to -1.0 as well).
"""
from __future__ import annotations

import io
import wave
from dataclasses import dataclass

import numpy as np

from .errors import InvalidInputError

SUPPORTED_SAMPLE_WIDTHS = (2, 3, 4)  # 16/24/32-bit PCM


@dataclass(frozen=True)
class PcmMono:
    samples: np.ndarray          # float64, 1-D
    sample_rate: int
    sample_width: int            # bytes (2/3/4)
    n_samples: int


def _decode_pcm(raw: bytes, width: int) -> np.ndarray:
    if width == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        signed = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        signed[signed >= 0x800000] -= 0x1000000
        a = signed.astype(np.float64) / 8388608.0
    else:  # pragma: no cover - guarded by caller
        raise InvalidInputError(
            f"unsupported PCM bit depth: {width * 8}",
            details={"sample_width_bytes": width},
        )
    return np.asarray(a, dtype=np.float64)


def _encode_pcm(samples: np.ndarray, width: int) -> bytes:
    if not np.all(np.isfinite(samples)):
        raise InvalidInputError("cannot encode non-finite PCM samples")
    clipped = np.clip(samples, -1.0, 1.0)
    if width == 2:
        q = np.rint(clipped * 32768.0)
        q = np.clip(q, -32768, 32767).astype("<i2")
        return q.tobytes()
    if width == 4:
        q = np.rint(clipped * 2147483648.0)
        q = np.clip(q, -2147483648, 2147483647).astype("<i4")
        return q.tobytes()
    if width == 3:
        q = np.rint(clipped * 8388608.0).astype(np.int64)
        q = np.clip(q, -8388608, 8388607)
        out = np.empty((q.size, 3), dtype=np.uint8)
        out[:, 0] = q & 0xFF
        out[:, 1] = (q >> 8) & 0xFF
        out[:, 2] = (q >> 16) & 0xFF
        return out.reshape(-1).tobytes()
    raise InvalidInputError(
        f"unsupported PCM bit depth: {width * 8}",
        details={"sample_width_bytes": width},
    )


def read_wav(source: str | bytes | io.IOBase) -> PcmMono:
    """Parse a mono PCM WAV from a path, raw bytes, or binary file object."""
    try:
        if isinstance(source, (bytes, bytearray)):
            wf = wave.open(io.BytesIO(bytes(source)), "rb")
        elif isinstance(source, str):
            wf = wave.open(source, "rb")
        else:
            pos = source.tell() if source.seekable() else None
            wf = wave.open(source, "rb")
            if pos is not None:
                source.seek(pos)
    except wave.Error as exc:
        raise InvalidInputError(
            f"not a valid WAV file: {exc}",
            details={"reason": "wave_parse_error"},
        ) from exc
    try:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        rate = wf.getframerate()
        comp = wf.getcomptype()
        n = wf.getnframes()
        if comp != "NONE":
            raise InvalidInputError(
                f"compressed WAV not supported (comptype={comp})",
                details={"comptype": comp},
            )
        if nch != 1:
            raise InvalidInputError(
                f"only mono PCM supported, got {nch} channels",
                details={"channels": nch},
            )
        if sw not in SUPPORTED_SAMPLE_WIDTHS:
            raise InvalidInputError(
                f"only 16/24/32-bit PCM supported, got {sw * 8}-bit",
                details={"sample_width_bytes": sw},
            )
        if rate <= 0:
            raise InvalidInputError(
                f"invalid sample rate {rate}", details={"sample_rate": rate})
        raw = wf.readframes(n)
    finally:
        wf.close()
    samples = _decode_pcm(raw, sw)
    if not np.all(np.isfinite(samples)):  # pragma: no cover - integer decode
        raise InvalidInputError("decoded PCM contains non-finite samples")
    return PcmMono(samples=samples, sample_rate=rate, sample_width=sw,
                   n_samples=samples.size)


def write_wav(samples: np.ndarray, sample_rate: int, *,
              sample_width: int = 2) -> bytes:
    """Serialize mono float samples (clipped to [-1, 1]) as PCM WAV bytes."""
    if samples.ndim != 1:
        raise InvalidInputError(
            "only mono output can be written",
            details={"ndim": samples.ndim})
    if sample_width not in SUPPORTED_SAMPLE_WIDTHS:
        raise InvalidInputError(
            f"only 16/24/32-bit PCM supported, got {sample_width * 8}-bit",
            details={"sample_width_bytes": sample_width})
    pcm = _encode_pcm(np.asarray(samples, dtype=np.float64), sample_width)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(sample_width)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm)
    return buf.getvalue()
