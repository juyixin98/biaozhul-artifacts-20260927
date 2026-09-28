"""Media boundary: PCM codecs, WAV parsing, non-finite input, overflow policy."""

from __future__ import annotations

import struct

import numpy as np
import pytest

from resampler.errors import InputValidationError, OutputOverflowError
from resampler.media import (decode_pcm, encode_pcm, parse_wav, build_wav,
                             sample_width)


@pytest.mark.parametrize("fmt", ["u8", "s16le", "s24le", "s32le"])
def test_integer_pcm_roundtrip_levels(fmt, runlog):
    max_int = {"u8": 127, "s16le": 32767, "s24le": (1 << 23) - 1,
               "s32le": 2147483647}[fmt]
    # Positive full scale for symmetric int formats is max_int/(max_int+1).
    pos_fs = max_int / (max_int + 1)
    levels = np.array([-1.0, -0.5, 0.0, 0.5, pos_fs])
    enc = encode_pcm(levels, fmt, clip_policy="reject")
    back = decode_pcm(enc.data, fmt)
    tol = 1.0 / (max_int + 1)
    max_err = float(np.max(np.abs(back - levels)))
    runlog.check(f"{fmt} roundtrip within 1 LSB", max_err <= tol * 1.01,
                 {"max_err": max_err, "tol": tol},
                 "nearest-level encoding, symmetric int rails")


def test_float_formats_roundtrip(runlog):
    x = np.array([-0.75, 0.0, 0.125, 0.999])
    for fmt in ["f32le", "f64le"]:
        back = decode_pcm(encode_pcm(x, fmt).data, fmt)
        runlog.check(f"{fmt} finite roundtrip", np.all(np.isfinite(back)),
                     {}, "no NaN/Inf introduced")


def test_u8_midpoint_encoding(runlog):
    enc = encode_pcm(np.zeros(2), "u8")
    runlog.check("u8 silence = 128 bytes", enc.data == bytes([128, 128]),
                 {"bytes": list(enc.data)}, "unsigned 8-bit center convention")


def test_s24_byte_order_and_sign(runlog):
    x = np.array([-1.0, 1.0 - 2 ** -23])
    enc = encode_pcm(x, "s24le")
    # -2^23 -> little endian 00 00 80 ; max positive -> ff ff 7f
    runlog.check("s24 negative full scale", enc.data[:3] == bytes([0, 0, 0x80]),
                 {"bytes": enc.data[:3].hex()}, "two's complement LE")
    runlog.check("s24 positive full scale", enc.data[3:] == bytes([0xFF, 0xFF, 0x7F]),
                 {"bytes": enc.data[3:].hex()}, "max positive 2^23-1")


def test_misaligned_bytes_rejected():
    with pytest.raises(InputValidationError) as ei:
        decode_pcm(b"\x00\x00\x01", "s16le")
    assert ei.value.category == "input_error"


def test_unknown_format_rejected():
    with pytest.raises(InputValidationError):
        decode_pcm(b"", "mp3")
    with pytest.raises(InputValidationError):
        encode_pcm(np.zeros(1), "s16be")


def test_nonfinite_pcm_float_rejected(runlog):
    raw = struct.pack("<ff", 1.0, float("nan"))
    with pytest.raises(InputValidationError) as ei:
        decode_pcm(raw, "f32le")
    runlog.check("NaN f32 payload -> input_error",
                 ei.value.category == "input_error", ei.value.detail,
                 "non-finite samples never enter the kernel")


def test_overflow_clip_counts_and_reject(runlog):
    x = np.array([0.9, 1.5, -2.0, 0.3])
    enc = encode_pcm(x, "s16le", clip_policy="clip")
    runlog.check("clip policy counts 2 saturated samples", enc.clipped == 2,
                 {"clipped": enc.clipped}, "+1.5 and -2.0 out of range")
    back = decode_pcm(enc.data, "s16le")
    runlog.check("saturated values at rails",
                 abs(back[1] - 1.0) < 1e-4 and abs(back[2] + 1.0) < 1e-4,
                 {"back": back.tolist()}, "saturate, not wrap")
    with pytest.raises(OutputOverflowError) as eo:
        encode_pcm(x, "s16le", clip_policy="reject")
    runlog.check("reject policy -> output_overflow (422 category)",
                 eo.value.category == "output_overflow", eo.value.detail,
                 "distinct from input and computation failures")


def test_nonfinite_encode_overflow(runlog):
    x = np.array([0.1, float("inf")])
    with pytest.raises(OutputOverflowError) as eo:
        encode_pcm(x, "f32le", clip_policy="reject")
    runlog.check("inf float64 -> f32 rejected",
                 eo.value.category == "output_overflow", eo.value.detail,
                 "even float encoding has a representable range")


def _wav(samples_f64, rate, fmt):
    return build_wav(samples_f64, rate, fmt)


def test_wav_parse_roundtrip_all_formats(runlog):
    x = np.array([-0.5, 0.0, 0.25, 0.5])
    for fmt in ["u8", "s16le", "s24le", "s32le", "f32le", "f64le"]:
        blob = _wav(x, 16000, fmt)
        dec = parse_wav(blob)
        runlog.check(f"WAV {fmt} parsed as mono PCM",
                     dec.info.channels == 1 and dec.info.n_frames == 4
                     and dec.info.sample_rate == 16000
                     and dec.info.pcm_format == fmt,
                     {"info": dec.info.__dict__}, "RIFF header roundtrip")
        runlog.check(f"WAV {fmt} samples within tolerance",
                     np.max(np.abs(dec.samples - x)) < 2e-3,
                     {"max_err": float(np.max(np.abs(dec.samples - x)))},
                     "integer formats quantize")


def test_wav_rejects_stereo_and_bad_magic():
    fmt_body = struct.pack("<HHIIHH", 1, 2, 8000, 16000, 2, 8)
    fmt_chunk = b"fmt " + struct.pack("<I", 16) + fmt_body
    data_chunk = b"data" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
    body = fmt_chunk + data_chunk
    stereo = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    with pytest.raises(InputValidationError) as ei:
        parse_wav(stereo)
    assert "mono" in ei.value.message

    with pytest.raises(InputValidationError) as ei:
        parse_wav(b"NOTWAV" + b"\x00" * 20)
    assert ei.value.category == "input_error"


def test_wav_rejects_compressed_tag():
    fmt_body = struct.pack("<HHIIHH", 0x55, 1, 8000, 8000, 1, 8)
    fmt_chunk = b"fmt " + struct.pack("<I", 16) + fmt_body
    data_chunk = b"data" + struct.pack("<I", 1) + b"\x80"
    body = fmt_chunk + data_chunk
    blob = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    with pytest.raises(InputValidationError) as ei:
        parse_wav(blob)
    assert "encoding" in ei.value.message


def test_wav_truncated_chunk_rejected():
    with pytest.raises(InputValidationError):
        parse_wav(b"RIFF" + struct.pack("<I", 9999) + b"WAVE" + b"fmt \x10\x00")
