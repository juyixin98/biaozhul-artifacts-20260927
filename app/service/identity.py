"""Runtime identity: run ids and component versions for log correlation."""
from __future__ import annotations

import platform
import sys
import uuid

import pyarrow as pa

from ..config import SORT_POLICY


def new_run_id() -> str:
    return uuid.uuid4().hex


def component_versions() -> dict[str, str]:
    import fastapi
    import pydantic

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "pyarrow": pa.__version__,
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
        "sort_policy": SORT_POLICY,
    }


def interpreter_info() -> str:
    return f"{sys.executable} py{platform.python_version()}"
