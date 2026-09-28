"""编解码公开入口（与 errors/types 同属 ABI 内核）。"""

from __future__ import annotations

from .decoder import decode
from .encoder import encode

__all__ = ["encode", "decode"]
