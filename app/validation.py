"""Typed input validation for measurement requests.

Every rejection carries a stable ``code`` so failure categories are
assertable by tests and greppable in logs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .media import RAW_PCM_FORMATS, LAYOUT_MONO, LAYOUT_STEREO, LAYOUT_50, LAYOUT_51
from .config import Settings

KNOWN_LAYOUTS = (LAYOUT_MONO, LAYOUT_STEREO, LAYOUT_50, LAYOUT_51)


class ValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RawPcmDescriptor:
    sample_rate: int
    channels: int
    sample_format: str
    layout: str | None
    include_blocks: bool
    label: str | None


def _as_bool(value: str | bool | None, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if value.lower() in ("1", "true", "yes", "on"):
        return True
    if value.lower() in ("0", "false", "no", "off"):
        return False
    raise ValidationError("INVALID_BOOL", f"{field} must be a boolean, got {value!r}")


def _as_int(value: str | int | None, field: str) -> int:
    if value is None or value == "":
        raise ValidationError("MISSING_PARAMETER", f"{field} is required")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError("INVALID_INTEGER", f"{field} must be an integer, got {value!r}")


def validate_raw_pcm_params(*, sample_rate, channels, sample_format, layout=None,
                            include_blocks=False, label=None,
                            payload_size: int | None = None,
                            settings: Settings | None = None) -> RawPcmDescriptor:
    settings = settings or Settings()

    rate = _as_int(sample_rate, "sample_rate")
    if rate not in settings.supported_sample_rates:
        raise ValidationError(
            "UNSUPPORTED_SAMPLE_RATE",
            f"sample_rate {rate} not in {list(settings.supported_sample_rates)}")

    n_ch = _as_int(channels, "channels")
    if n_ch not in (1, 2, 5, 6):
        raise ValidationError(
            "UNSUPPORTED_CHANNEL_LAYOUT",
            f"channels {n_ch} not supported (1/2/5/6 -> mono/stereo/5.0/5.1)")

    fmt = (sample_format or "").lower()
    if fmt not in RAW_PCM_FORMATS:
        raise ValidationError(
            "UNSUPPORTED_PCM_FORMAT",
            f"sample_format {sample_format!r} not in {list(RAW_PCM_FORMATS)}")

    if layout is not None and layout != "":
        if layout not in KNOWN_LAYOUTS:
            raise ValidationError("UNSUPPORTED_LAYOUT",
                                  f"layout {layout!r} not in {list(KNOWN_LAYOUTS)}")
        physical = {LAYOUT_MONO: 1, LAYOUT_STEREO: 2, LAYOUT_50: 5, LAYOUT_51: 6}[layout]
        if physical != n_ch:
            raise ValidationError(
                "LAYOUT_CHANNEL_MISMATCH",
                f"layout {layout} requires {physical} channels but channels={n_ch}")
        resolved_layout = layout
    else:
        resolved_layout = None  # media layer infers from channel count

    include = _as_bool(include_blocks, "include_blocks")
    if payload_size is not None and payload_size == 0:
        raise ValidationError("EMPTY_PAYLOAD", "no PCM bytes were received")
    if payload_size is not None and payload_size > settings.max_payload_bytes:
        raise ValidationError(
            "PAYLOAD_TOO_LARGE",
            f"payload {payload_size} bytes exceeds limit {settings.max_payload_bytes}")

    return RawPcmDescriptor(
        sample_rate=rate, channels=n_ch, sample_format=fmt,
        layout=resolved_layout, include_blocks=include, label=label)
