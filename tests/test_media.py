"""WAV media boundary tests: integer PCM decode/encode for 16/24/32 bit."""
from __future__ import annotations

import io
import math
import wave

import numpy as np
import pytest

from resamp.errors import InvalidInputError
from resamp.media import read_wav, write_wav


def _make_wav(samples_int, rate, width):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples_int).tobytes())
    return buf.getvalue()


@pytest.mark.parametrize("width,fullcode,dtype", [
    (2, 32768, "<i2"),
    (4, 2147483648, "<i4"),
])
def test_int_pcm_roundtrip_16_32(width, fullcode, dtype):
    maxpos = fullcode - 1
    codes = np.array([0, 1, -1, maxpos, -fullcode, 12345, -12345])
    blob = _make_wav(codes.astype(dtype), 16000, width)
    pcm = read_wav(blob)
    assert pcm.sample_rate == 16000
    assert pcm.sample_width == width
    assert pcm.samples.size == codes.size
    # Symmetric scaling: code c decodes to c/2^(b-1) and re-encodes exactly.
    assert np.rint(pcm.samples * fullcode).astype(np.int64).tolist() == \
        codes.astype(np.int64).tolist()
    back_blob = write_wav(pcm.samples, 16000, sample_width=width)
    pcm2 = read_wav(back_blob)
    assert np.array_equal(pcm2.samples, pcm.samples)
    # +1.0 clips to the maximal positive code; -1.0 is the exact negative code.
    extreme = read_wav(write_wav(np.array([-1.0, 1.0]), 16000,
                                 sample_width=width))
    assert np.rint(extreme.samples * fullcode).tolist() == [-fullcode, maxpos]


def test_24bit_decode_encode_explicit_codes():
    codes = [0, 1, -1, 8388607, -8388608, 123456, -123456]
    packed = b"".join(int(c % (1 << 24)).to_bytes(3, "little",
                                                  signed=False) for c in codes)
    blob = _make_wav(packed, 24000, 3)
    pcm = read_wav(blob)
    assert pcm.sample_width == 3
    decoded_codes = np.rint(pcm.samples * 8388608.0).astype(np.int64)
    assert decoded_codes.tolist() == codes
    blob2 = write_wav(pcm.samples, 24000, sample_width=3)
    pcm2 = read_wav(blob2)
    assert np.array_equal(pcm2.samples, pcm.samples)
    extreme = read_wav(write_wav(np.array([-1.0, 1.0]), 24000, sample_width=3))
    assert np.rint(extreme.samples * 8388608.0).tolist() == [-8388608,
                                                              8388607]


def test_stereo_rejected():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(np.zeros(20, dtype="<i2").tobytes())
    with pytest.raises(InvalidInputError) as ei:
        read_wav(buf.getvalue())
    assert "mono" in ei.value.message


def test_eight_bit_rejected():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(1)
        wf.setframerate(8000)
        wf.writeframes(bytes(10))
    with pytest.raises(InvalidInputError):
        read_wav(buf.getvalue())


def test_garbage_rejected():
    with pytest.raises(InvalidInputError):
        read_wav(b"not a wav file at all" * 20)


def test_encode_clips_overflow():
    x = np.array([-1.5, 0.0, 1.5])
    blob = write_wav(x, 8000, sample_width=2)
    pcm = read_wav(blob)
    assert pcm.samples[0] == pytest.approx(-1.0, abs=1 / 32767)
    assert pcm.samples[2] == pytest.approx(1.0, abs=1 / 32767)


def test_encoded_wav_resamples_end_to_end():
    """A 16 kHz sine WAV resampled to 48 kHz at sample level is coherent."""
    from resamp.dsp.polyphase import PolyphaseResampler
    from resamp.dsp.reference import resample_offline
    x = 0.8 * np.sin(2 * np.pi * 1000 * np.arange(16000) / 16000)
    pcm_codes = np.rint(x * 32767).astype("<i2")
    pcm = read_wav(_make_wav(pcm_codes, 16000, 2))
    y, design = resample_offline(pcm.samples, 16000, 48000)
    out_blob = write_wav(y, 48000, sample_width=2)
    out = read_wav(out_blob)
    assert out.sample_rate == 48000
    # Count follows the documented formula, not the nominal rate ratio.
    expected = PolyphaseResampler.expected_output_count(
        16000, design.ratio, design)
    assert out.samples.size == expected
    assert abs(out.samples.size - 48000) <= design.half + 5
