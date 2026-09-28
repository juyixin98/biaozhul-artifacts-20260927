"""Runtime versions surfaced by the health endpoint and written to every log line."""

from __future__ import annotations

import platform
import sys

import pyarrow as pa

from arrowzero import __version__


def runtime_versions() -> dict[str, str]:
    return {
        "arrowzero": __version__,
        "python": sys.version.split()[0],
        "pyarrow": pa.__version__,
        "platform": platform.platform(),
    }
