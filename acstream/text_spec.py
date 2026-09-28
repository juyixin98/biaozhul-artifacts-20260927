"""文本规范：线上字节载荷的编码与位置约定。

- 匹配的唯一权威表示是原始字节（bytes），所有命中位置都是该字节流内的
  半开区间 [start, end)，起始为 0；
- 客户端可通过 ``encoding`` 选择传输编码：
    * "utf-8"（默认）：data 为 JSON 字符串；空串表示零字节；
    * "base64" / "hex"：data 为字符串，按标准解码；空串表示零字节；
    * "raw"：data 为 JSON 字符串，按 latin-1 逐字节取（便于调试二进制，
      每个码点必须 <= 255）；
- 模式集合上传使用同样的编码，但在版本级别统一指定。

解码失败一律归类为 ENCODING_ERROR（reject）：服务端可以确定它非法。
"""

from __future__ import annotations

import base64
import binascii

from .errors import ApiError, ErrorCode

SUPPORTED_ENCODINGS = ("utf-8", "base64", "hex", "raw")


def decode_payload(data: str, encoding: str, *, field: str = "data") -> bytes:
    if not isinstance(data, str):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            422,
            f"{field} 必须是字符串（{encoding} 文本载荷）",
        )
    try:
        if encoding == "utf-8":
            return data.encode("utf-8")
        if encoding == "base64":
            # validate=True：拒绝非字母表字符，防止静默截断。
            return base64.b64decode(data, validate=True)
        if encoding == "hex":
            return bytes.fromhex(data)
        if encoding == "raw":
            return data.encode("latin-1")
    except (ValueError, binascii.Error, UnicodeEncodeError) as exc:
        raise ApiError(
            ErrorCode.ENCODING_ERROR,
            422,
            f"{field} 无法按 {encoding} 解码：{exc}",
            details={"encoding": encoding},
        ) from exc

    raise ApiError(
        ErrorCode.VALIDATION_ERROR,
        422,
        f"不支持的编码 {encoding!r}，支持：{', '.join(SUPPORTED_ENCODINGS)}",
        details={"supported": list(SUPPORTED_ENCODINGS)},
    )


def preview_hex(data: bytes, limit: int = 16) -> str:
    """供诊断使用的脱敏预览：最多 limit 字节 hex + 长度。"""
    shown = data[:limit].hex()
    if len(data) > limit:
        shown += "…"
    return f"<bytes len={len(data)} hex={shown}>"
