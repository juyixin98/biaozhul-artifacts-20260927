"""clockalign: two-track audio clock drift estimation and timeline correction."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:  # installed via pip -e, otherwise fall back to the pyproject version
    __version__ = version("clockalign")
except PackageNotFoundError:  # pragma: no cover - exercised only when not installed
    __version__ = "1.0.0"

SERVICE = "clockalign"
