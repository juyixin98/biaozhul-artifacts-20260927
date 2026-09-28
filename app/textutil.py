"""文本规范与码点偏移 ↔ UTF-8 原始字节范围转换。

文本规范
--------
* 规范文本是 Python ``str``（Unicode 码点序列）；持久化使用 UTF-8。
* 所有正则匹配位置都是 **码点偏移** 半开区间 ``[char_start, char_end)``。
* 对外同时给出 **原始字节范围**（UTF-8 字节偏移），并在计划前验证字节范围
  切片恰好等于匹配到的码点切片，避免多字节边界错误。

为什么不能 ``len(text.encode())`` 直接换算
------------------------------------------
多字节文本上，码点偏移与字节偏移非线性相关。这里对大文本采用 **块抽样表**：
扫描一次 UTF-8 字节，每 ``block`` 个码点记录一个锚点 (char_index, byte_index)，
单条范围换算只需扫描一个块。空间 O(chars/block)，时间 O(block)。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .errors import TextNotUnicodeError


@dataclass(frozen=True)
class TextSpec:
    """源文本的规范摘要：绑定计划与版本守卫的唯一依据。"""

    sha256: str
    byte_len: int
    char_len: int

    def as_tuple(self) -> tuple[str, int, int]:
        return (self.sha256, self.byte_len, self.char_len)


def normalize_text(value: str | bytes) -> str:
    """把入参归一化为规范 ``str``；拒绝非法 Unicode。"""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes | bytearray):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TextNotUnicodeError(
                "source bytes are not valid UTF-8",
                details={"reason": str(exc), "byte": getattr(exc, "start", None)},
            ) from exc
    raise TextNotUnicodeError(
        "text must be str or UTF-8 bytes",
        details={"received_type": type(value).__name__},
    )


def make_spec(text: str) -> TextSpec:
    encoded = text.encode("utf-8")
    return TextSpec(
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_len=len(encoded),
        char_len=len(text),
    )


class ByteOffsetMap:
    """码点偏移 → UTF-8 字节偏移映射。

    * 纯 ASCII：字节偏移恒等于码点偏移，零额外内存。
    * 多字节：构建一次 **逐码点前缀字节长度表** ``_prefix[i]``（码点 i 之前
      的字节数），把转换做成 O(1)。大文本上零宽模式会逐码点查询，块锚点的
      O(block) 线性回退会退化成 O(n·block)，故这里用空间换确定性时间。
      表为 Python int 列表，5M 码点量级约百 MB 级，在默认预算文本范围内；
      构造本身只扫一遍文本，O(n)。
    """

    def __init__(self, text: str, block: int = 4096):
        if block < 1:
            raise ValueError("block must be >= 1")
        self._text = text
        self._block = block
        # 全文字节只编码一次并缓存：verify 在大量（零宽）命中时会被高频调用，
        # 若每次重编将退化为 O(n²)。
        encoded = text.encode("utf-8")
        self._encoded = encoded
        self._total_bytes = len(encoded)
        self._total_chars = len(text)
        # 快路：纯 ASCII 时直接恒等
        self._ascii = self._total_bytes == self._total_chars
        # 前缀表：_prefix[i] = 码点 i 之前的字节数；长度 = char_len+1
        self._prefix: list[int] | None = None
        if not self._ascii:
            prefix = [0] * (self._total_chars + 1)
            byte_i = 0
            for i, ch in enumerate(text):
                prefix[i] = byte_i
                byte_i += len(ch.encode("utf-8"))
            prefix[self._total_chars] = byte_i
            self._prefix = prefix

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def total_chars(self) -> int:
        return self._total_chars

    def char_to_byte(self, char_index: int) -> int:
        """把码点偏移转换为 UTF-8 字节偏移（允许等于长度，表切片末端）。"""
        if not 0 <= char_index <= self._total_chars:
            raise ValueError(f"char index out of range: {char_index}")
        if self._ascii or self._prefix is None:
            return char_index
        return self._prefix[char_index]

    def char_span_to_byte_span(self, start: int, end: int) -> tuple[int, int]:
        return self.char_to_byte(start), self.char_byte_end(start, end)

    def char_byte_end(self, start: int, end: int) -> int:
        """字节末端。利用“匹配切片”的字节长度，避免从 0 重扫到 end。"""
        if not 0 <= start <= end <= self._total_chars:
            raise ValueError(f"char span out of range: [{start},{end})")
        if self._ascii:
            return end
        return self.char_to_byte(end)

    def verify_byte_span(self, start: int, end: int) -> tuple[int, int]:
        """验证字节范围与码点范围一致，返回字节半开区间。

        验证方式：转换后按字节切回 UTF-8 解码，必须等于码点切片。
        任何不一致都抛 ``ValueError``（由上层包成输入/计算错误）。
        """
        b0 = self.char_to_byte(start)
        b1 = self.char_byte_end(start, end)
        slice_bytes = self._encoded[b0:b1]
        try:
            decoded = slice_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"byte span is not a UTF-8 boundary: {exc}") from exc
        if decoded != self._text[start:end]:
            raise ValueError(
                f"byte/char span mismatch: [{b0},{b1}) decodes to {decoded!r}, "
                f"expected {self._text[start:end]!r}"
            )
        return b0, b1
