"""文本规范层。

职责（对应需求“文本规范”边界）：
- 严格的 UTF-8 合法性判定（Unicode D92 well-formed），拒绝非法字节，
  定位首个非法偏移并区分失败原因（截断/非法前导/非法延续/过长假编码/代理码点/超范围）；
- 字节 → 码点解码与文本统计（字节数、码点数、CRLF、BOM）；
- NFC 规范化（规范化不是隐式重写：索引始终绑定原始字节，NFC 仅作为诊断与显式选项）；
- 原文 SHA-256 摘要（索引版本绑定它，不绑定字符计数）。
"""
from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

from .errors import InvalidUtf8Error

# UTF-8 编码结构表：首字节 → (字节总长, 载荷高位下限掩码比较)
# 直接按 Unicode 标准 D92/D92a/D92b/D88/D99 判定，拒绝过长假编码与代理码点。


def validate_utf8(data: bytes) -> None:
    """按 Unicode 标准严格校验；非法时抛 InvalidUtf8Error（带 offset/reason）。"""
    i = 0
    n = len(data)
    while i < n:
        b0 = data[i]
        if b0 <= 0x7F:
            i += 1
            continue
        if 0x80 <= b0 <= 0xBF:
            raise InvalidUtf8Error(
                "UTF-8 解码失败：延续字节出现在序列起点",
                details={"offset": i, "byte": b0, "reason": "unexpected_continuation"},
            )
        if 0xC2 <= b0 <= 0xDF:
            width, min_cp, max_cp = 2, 0x80, 0x7FF
        elif 0xC0 <= b0 <= 0xC1:
            # 形态合法（2 字节）但解码值必然 < 0x80，按过长假编码处理
            width, min_cp, max_cp = 2, 0x80, 0x7FF
        elif b0 == 0xE0:
            width, min_cp, max_cp = 3, 0x800, 0xFFFF
        elif 0xE1 <= b0 <= 0xEC:
            width, min_cp, max_cp = 3, 0x1000, 0xFFFF
        elif b0 == 0xED:
            # D99：U+D800..U+DFFF 代理码点非法 → 第二字节必须 <= 0x9F
            width, min_cp, max_cp = 3, 0xD000, 0xD7FF
        elif 0xEE <= b0 <= 0xEF:
            width, min_cp, max_cp = 3, 0xE000, 0xFFFF
        elif b0 == 0xF0:
            width, min_cp, max_cp = 4, 0x10000, 0x3FFFF
        elif 0xF1 <= b0 <= 0xF3:
            width, min_cp, max_cp = 4, 0x40000, 0xFFFFF
        elif b0 == 0xF4:
            # 码点不得超过 U+10FFFF → 第二字节 <= 0x8F
            width, min_cp, max_cp = 4, 0x100000, 0x10FFFF
        else:
            raise InvalidUtf8Error(
                "UTF-8 解码失败：非法前导字节",
                details={"offset": i, "byte": b0, "reason": "invalid_lead_byte"},
            )

        seq = data[i : i + width]
        if len(seq) < width:
            raise InvalidUtf8Error(
                "UTF-8 解码失败：序列在延续字节处截断",
                details={
                    "offset": i,
                    "reason": "truncated_sequence",
                    "expected": width,
                    "actual": len(seq),
                },
            )
        cp = b0 & (0x7F >> width)
        for k in range(1, width):
            bk = seq[k]
            if not 0x80 <= bk <= 0xBF:
                raise InvalidUtf8Error(
                    "UTF-8 解码失败：非法延续字节",
                    details={"offset": i + k, "byte": bk,
                             "reason": "invalid_continuation"},
                )
            # 即使形态是延续字节，某些前导字节的第一个延续字节受限
            # （过长假编码 / 代理码点 / 超 U+10FFFF），错误定位在该延续字节
            if k == 1:
                if b0 in (0xC0, 0xC1) or (b0 == 0xE0 and bk < 0xA0) or (
                    b0 == 0xF0 and bk < 0x90
                ):
                    reason = "overlong_encoding"
                elif b0 == 0xED and bk > 0x9F:
                    reason = "surrogate_codepoint"
                elif b0 == 0xF4 and bk > 0x8F:
                    reason = "codepoint_out_of_range"
                else:
                    reason = None
                if reason is not None:
                    raise InvalidUtf8Error(
                        "UTF-8 解码失败：受限范围违规",
                        details={"offset": i + k, "byte": bk, "reason": reason},
                    )
            cp = (cp << 6) | (bk & 0x3F)
        if not min_cp <= cp <= max_cp:
            if 0xD800 <= cp <= 0xDFFF:
                reason = "surrogate_codepoint"
            elif cp < min_cp:
                reason = "overlong_encoding"
            else:
                reason = "codepoint_out_of_range"
            raise InvalidUtf8Error(
                "UTF-8 解码失败：码点不合法",
                details={"offset": i, "codepoint": cp, "reason": reason},
            )
        i += width


def decode_strict(data: bytes) -> str:
    """先自实现校验，再走标准解码（双重保证；标准解码不应再失败）。"""
    validate_utf8(data)
    try:
        return data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # 理论不可达：自实现校验已通过
        raise InvalidUtf8Error(
            "UTF-8 标准解码失败",
            details={"offset": exc.start, "reason": "stdlib_decode_error"},
        ) from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def nfc_normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text)


@dataclass(frozen=True)
class TextStats:
    byte_count: int
    codepoint_count: int
    has_crlf: bool
    has_bom: bool
    nfc_identical: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "byte_count": self.byte_count,
            "codepoint_count": self.codepoint_count,
            "has_crlf": self.has_crlf,
            "has_bom": self.has_bom,
            "nfc_identical": self.nfc_identical,
        }


def compute_stats(text: str, raw: bytes) -> TextStats:
    return TextStats(
        byte_count=len(raw),
        codepoint_count=len(text),
        has_crlf="\r\n" in text,
        has_bom=text.startswith("\ufeff"),
        nfc_identical=nfc_normalize(text) == text,
    )
