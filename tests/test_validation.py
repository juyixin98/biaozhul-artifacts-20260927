"""Validation tests assert specific failure codes, not generic 4xx behavior."""

from __future__ import annotations

import pytest

from app.validation import ValidationError, validate_raw_pcm_params


def _call(**overrides):
    params = dict(sample_rate=48000, channels=1, sample_format="s16",
                  payload_size=1000)
    params.update(overrides)
    return validate_raw_pcm_params(**params)


def test_valid_descriptor_passes():
    d = _call(channels=6, layout="5.1", include_blocks="true", label="lab")
    assert d.sample_rate == 48000
    assert d.channels == 6
    assert d.layout == "5.1"
    assert d.include_blocks is True
    assert d.label == "lab"


@pytest.mark.parametrize("field,value,code", [
    ("sample_rate", 11025, "UNSUPPORTED_SAMPLE_RATE"),
    ("sample_rate", "abc", "INVALID_INTEGER"),
    ("channels", 3, "UNSUPPORTED_CHANNEL_LAYOUT"),
    ("sample_format", "s8", "UNSUPPORTED_PCM_FORMAT"),
    ("layout", "atmos", "UNSUPPORTED_LAYOUT"),
    ("include_blocks", "maybe", "INVALID_BOOL"),
])
def test_invalid_params_fail_with_codes(field, value, code):
    with pytest.raises(ValidationError) as exc:
        _call(**{field: value})
    assert exc.value.code == code


def test_layout_channel_mismatch_is_its_own_category():
    with pytest.raises(ValidationError) as exc:
        _call(channels=2, layout="5.1")
    assert exc.value.code == "LAYOUT_CHANNEL_MISMATCH"


def test_empty_and_oversized_payload():
    with pytest.raises(ValidationError) as exc:
        _call(payload_size=0)
    assert exc.value.code == "EMPTY_PAYLOAD"
    with pytest.raises(ValidationError) as exc:
        _call(payload_size=65 * 1024 * 1024)
    assert exc.value.code == "PAYLOAD_TOO_LARGE"
