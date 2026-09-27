"""Media parsing: WAV container demux and float32 sample conversion.

Only uncompressed PCM WAV (8/16/24/32-bit) is read -- enough for the local
fixtures, and every refusal is explicit. This module deliberately contains no
clock/signal math: it answers "what samples, at what nominal rate", nothing
about whether the rate is honest.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .errors import MediaError

_SUPPORTED_SAMPLE_WIDTHS = (1, 2, 3, 4)


@dataclass(frozen=True)
class AudioTrack:
    """One decoded mono track and its *nominal* sample rate.

    The nominal rate is the rate claimed by the container; the drift estimator
    exists precisely because the device clock may not honor it.
    """

    samples: np.ndarray  # float32 in [-1, 1]
    sample_rate: int
    source: str
    channel: int | None = None  # 0-based channel when extracted from stereo

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate


def _decode_pcm(raw: bytes, sampwidth: int, nchannels: int,
                nframes: int) -> np.ndarray:
    if sampwidth == 1:  # WAV 8-bit is unsigned
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        data = (data - 128.0) / 128.0
    elif sampwidth == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif sampwidth == 3:
        # 24-bit little-endian packed PCM -> int32 with the low byte zero
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        i32 = (b[:, 0].astype(np.int32)
               | (b[:, 1].astype(np.int32) << 8)
               | (b[:, 2].astype(np.int32) << 16))
        i32 = np.where(i32 & 0x800000, i32 - 0x1000000, i32)
        data = i32.astype(np.float32) / 8388608.0
    else:  # pragma: no cover - guarded by caller
        raise MediaError(f"unsupported sample width: {sampwidth} bytes")
    return data.reshape(nframes, nchannels) if nchannels > 1 else data


def read_wav(path: str | Path) -> tuple[np.ndarray, int, int]:
    """Read a PCM WAV. Returns (samples[frames, channels] float32, rate, ch)."""
    path = Path(path)
    if not path.exists():
        raise MediaError(f"audio file not found: {path}",
                         details={"path": str(path)})
    try:
        with wave.open(str(path), "rb") as wf:
            nchannels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
            comp = wf.getcomptype()
    except wave.Error as exc:
        raise MediaError(f"could not parse WAV {path}: {exc}",
                         details={"path": str(path)}) from exc
    if comp != "NONE":
        raise MediaError(f"compressed WAV is not supported: {comp}",
                         details={"path": str(path)})
    if sampwidth not in _SUPPORTED_SAMPLE_WIDTHS:
        raise MediaError(f"unsupported sample width {sampwidth} bytes",
                         details={"path": str(path)})
    if rate <= 0:
        raise MediaError(f"invalid sample rate {rate}",
                         details={"path": str(path)})
    samples = _decode_pcm(raw, sampwidth, nchannels, nframes)
    if samples.ndim == 1 and nchannels > 1:
        samples = samples.reshape(nframes, nchannels)
    return samples, rate, nchannels


def load_track(path: str | Path, channel: int | None = None) -> AudioTrack:
    """Load one mono track, optionally extracting one channel of a multichannel
    file."""
    samples, rate, nchannels = read_wav(path)
    if nchannels == 1:
        mono = samples
        used_channel = 0
    elif channel is None:
        raise MediaError(
            f"{path} has {nchannels} channels but no channel index was given",
            details={"channels": nchannels})
    else:
        if not 0 <= channel < nchannels:
            raise MediaError(
                f"channel {channel} out of range for {nchannels}-channel file",
                details={"channels": nchannels, "requested": channel})
        mono = samples[:, channel]
        used_channel = channel
    return AudioTrack(samples=np.asarray(mono, dtype=np.float32),
                      sample_rate=rate, source=str(path), channel=used_channel)


def load_pair(
    reference_path: str | Path | None,
    slave_path: str | Path | None,
    *,
    stereo_path: str | Path | None = None,
    stereo_role: tuple[str, str] = ("reference", "slave"),
    max_sample_rate: int = 192000,
    sample_rate_mismatch_ppm_max: float = 50000.0,
) -> tuple[AudioTrack, AudioTrack]:
    """Resolve the two input tracks and validate their nominal format.

    Either two mono files are given, or one stereo file whose channel role is
    given by ``stereo_role``.
    """
    if stereo_path is not None:
        if reference_path is not None or slave_path is not None:
            raise MediaError(
                "provide either a stereo file or two mono files, not both")
        try:
            ref_idx = stereo_role.index("reference")
            slv_idx = stereo_role.index("slave")
        except ValueError as exc:
            raise MediaError(
                f"stereo_channel_role must name reference and slave, got "
                f"{stereo_role}") from exc
        reference = load_track(stereo_path, ref_idx)
        slave = load_track(stereo_path, slv_idx)
    else:
        if reference_path is None or slave_path is None:
            raise MediaError(
                "two mono input files are required when no stereo file is given")
        reference = load_track(reference_path, 0)
        slave = load_track(slave_path, 0)

    for track, label in ((reference, "reference"), (slave, "slave")):
        if track.sample_rate > max_sample_rate:
            raise MediaError(
                f"{label} sample rate {track.sample_rate} exceeds maximum "
                f"{max_sample_rate}")
        if track.duration_s <= 0:
            raise MediaError(f"{label} track is empty")
    mismatch_ppm = (abs(reference.sample_rate - slave.sample_rate)
                    / reference.sample_rate * 1e6)
    if mismatch_ppm > sample_rate_mismatch_ppm_max:
        raise MediaError(
            f"nominal sample-rate mismatch {mismatch_ppm:.0f} ppm exceeds "
            f"{sample_rate_mismatch_ppm_max:.0f} ppm; refusing to treat a "
            "format mismatch as clock drift",
            details={"reference_rate": reference.sample_rate,
                     "slave_rate": slave.sample_rate})
    return reference, slave


def write_pcm_wav(path: str | Path, samples: np.ndarray, sample_rate: int,
                  *, sampwidth: int = 2) -> None:
    """Write mono or interleaved multi-channel float samples as PCM WAV."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(samples, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr[:, None]
    nframes, nchannels = arr.shape
    clipped = np.clip(arr, -1.0, 1.0)
    if sampwidth == 2:
        pcms = (clipped * 32767.0).round().astype("<i2")
    elif sampwidth == 1:
        pcms = ((clipped * 127.0) + 128.0).round().astype(np.uint8)
    elif sampwidth == 4:
        pcms = (clipped * 2147483647.0).round().astype("<i4")
    elif sampwidth == 3:
        i32 = np.clip(clipped * 8388607.0, -8388608.0,
                      8388607.0).round().astype("<i4")
        flat = i32.reshape(-1).astype(np.uint32)
        pcms = np.empty((flat.size, 3), dtype=np.uint8)
        pcms[:, 0] = flat & 0xFF
        pcms[:, 1] = (flat >> 8) & 0xFF
        pcms[:, 2] = (flat >> 16) & 0xFF
        pcms = pcms.reshape(nframes, nchannels, 3)
    else:
        raise MediaError(f"unsupported output sample width {sampwidth}")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(nchannels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcms.reshape(-1).tobytes())
