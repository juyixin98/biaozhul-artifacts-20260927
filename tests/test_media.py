"""媒体解析测试：支持矩阵、拒绝类别、WAV 往返与 NaN（计算失败而非解析失败）。"""

from __future__ import annotations

import io
import struct
import wave

import numpy as np
import pytest

from app.errors import SegmentError
from app.media import parse_audio


def _wav_bytes(samples: np.ndarray, *, rate: int = 8000, sampwidth: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def _float_wav_bytes(samples: np.ndarray, *, rate: int = 8000) -> bytes:
    # wave 模块不支持 float 容器，手写最小 RIFF(fmt tag=3, f32le)。
    data = samples.astype("<f4").tobytes()
    fmt = struct.pack("<HHIIHH", 3, 1, rate, rate * 4, 4, 32)
    def chunk(tag: bytes, payload: bytes) -> bytes:
        pad = b"\x00" if len(payload) % 2 else b""
        return tag + struct.pack("<I", len(payload)) + payload + pad

    return (
        b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data))
        + b"WAVE" + chunk(b"fmt ", fmt) + chunk(b"data", data)
    )


class TestWav:
    def test_s16_roundtrip(self):
        src = np.array([0, 16384, -16384, 32767, -32768], dtype="<i2")
        audio = parse_audio(_wav_bytes(src), sample_rate=None)
        assert audio.sample_rate == 8000
        assert audio.total_samples == 5
        assert audio.source_format == "wav:s16"
        np.testing.assert_allclose(
            audio.samples,
            [0.0, 16384 / 32768, -16384 / 32768, 32767 / 32768, -1.0],
            rtol=1e-4,
        )

    def test_f32_roundtrip(self):
        src = np.array([0.0, 0.25, -0.75], dtype=np.float32)
        audio = parse_audio(_float_wav_bytes(src))
        assert audio.source_format == "wav:f32"
        np.testing.assert_allclose(audio.samples, [0.0, 0.25, -0.75], rtol=1e-6)

    def test_sample_rate_mismatch_rejected(self):
        src = np.zeros(10, dtype="<i2")
        with pytest.raises(SegmentError) as ei:
            parse_audio(_wav_bytes(src, rate=8000), sample_rate=16000)
        assert ei.value.code == "MEDIA_PARSE_ERROR"
        assert ei.value.category == "input"

    def test_stereo_rejected(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(np.zeros(20, dtype="<i2").tobytes())
        with pytest.raises(SegmentError) as ei:
            parse_audio(buf.getvalue())
        assert ei.value.code == "MEDIA_PARSE_ERROR"
        assert ei.value.details["channels"] == 2

    def test_truncated_riff_rejected(self):
        with pytest.raises(SegmentError) as ei:
            parse_audio(b"RIFF\xff\xff\xff\xffWAVEgarbage")
        assert ei.value.code == "MEDIA_PARSE_ERROR"

    def test_not_wav_rejected_when_wav_forced(self):
        with pytest.raises(SegmentError) as ei:
            parse_audio(b"not a wav file at all", fmt="wav")
        assert ei.value.code == "MEDIA_PARSE_ERROR"


class TestRawPcm:
    def test_s16le_default_for_non_riff(self):
        src = np.array([0, 16384, -16384], dtype="<i2").tobytes()
        audio = parse_audio(src, sample_rate=16000)
        assert audio.source_format == "raw:s16le"
        assert audio.sample_rate == 16000
        assert audio.total_samples == 3

    def test_odd_byte_length_rejected(self):
        with pytest.raises(SegmentError) as ei:
            parse_audio(b"\x00\x00\x01", fmt="raw:s16le", sample_rate=8000)
        assert ei.value.code == "MEDIA_PARSE_ERROR"
        assert ei.value.details["trailing_bytes"] == 1

    def test_raw_requires_sample_rate(self):
        with pytest.raises(SegmentError) as ei:
            parse_audio(np.zeros(4, dtype="<i2").tobytes(), fmt="raw:s16le")
        assert ei.value.code == "INVALID_ARGUMENT"
        assert ei.value.details["field"] == "sample_rate"

    def test_unknown_codec_rejected(self):
        with pytest.raises(SegmentError) as ei:
            parse_audio(b"\x00" * 8, fmt="raw:mulaw", sample_rate=8000)
        assert ei.value.code == "MEDIA_PARSE_ERROR"

    def test_f32le_nan_passes_parse_fails_kernel(self):
        # 关键区分：NaN 不是 MEDIA_PARSE_ERROR，而是内核 COMPUTATION_FAILED。
        raw = np.array([0.0, np.nan, 0.5], dtype="<f4").tobytes()
        audio = parse_audio(raw, fmt="raw:f32le", sample_rate=8000)
        assert audio.total_samples == 3
        from app.segmentation import Params, SilenceSegmenter

        with pytest.raises(SegmentError) as ei:
            SilenceSegmenter(
                Params(8000, 0.02, 0.05, 10, 10, 0, 0)
            ).process(audio.samples)
        assert ei.value.code == "COMPUTATION_FAILED"
        assert ei.value.details["sample_index"] == 1


class TestEmpty:
    def test_empty_bytes(self):
        with pytest.raises(SegmentError) as ei:
            parse_audio(b"")
        assert ei.value.code == "EMPTY_INPUT"
        assert ei.value.category == "input"

    def test_wav_with_zero_samples(self):
        with pytest.raises(SegmentError) as ei:
            parse_audio(_wav_bytes(np.zeros(0, dtype="<i2")))
        assert ei.value.code == "EMPTY_INPUT"
