"""受限 ABI 类型系统与解析器。

支持范围（本后端的“受限类型”）：
    int<N> / uint<N>   N 为 8..256 的 8 的倍数
    bytes<N>           1..32 的定长字节串
    bytes              动态字节串
    string             动态 UTF-8 字符串
    T[k]               定长数组，k >= 1
    T[]                动态数组，可任意嵌套
    (T1,T2,...)        元组，可嵌套

明确不支持（解析期报 UnsupportedTypeError）：
    address, bool, fixed/ufixed, function, 带空格的 tuple 关键字形式
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import InvalidTypeError, UnsupportedTypeError

WORD = 32
MAX_DEPTH = 256
# 解码/编码硬上限：防止恶意 length/offset 触发巨大分配。
# 1 MiB 对本地受限后端绰绰有余；可通过环境变量覆盖（见 app/config.py）。
MAX_DECODE_BYTES = 1 << 20
MAX_ARRAY_ELEMENTS = (1 << 20) // WORD  # 单次数组最多 ~32768 个静态元素
MAX_BYTES_LENGTH = (1 << 20) - WORD

_SUPPORTED_DYNAMIC_ELEMENTARY = {"bytes", "string"}
_INT_RE = re.compile(r"^(u?int)(\d+)$")
_BYTESN_RE = re.compile(r"^bytes(\d+)$")
# 仅用于选择器签名的宽松基元集（见 call.py 的说明）
_SELECTOR_EXTRA_ELEMENTARY = {"address", "bool"}


@dataclass(frozen=True)
class AbiType:
    name: str  # 规范字符串，如 "(uint256,bytes[])[]"


@dataclass(frozen=True)
class ElementaryType(AbiType):
    kind: str = ""  # 'int' | 'uint' | 'bytesN' | 'bytes' | 'string'
    bits: int = 0  # 整数位宽；bytesN 时 N 放在 bits
    signed: bool = False

    @property
    def is_dynamic(self) -> bool:
        return self.kind in ("bytes", "string")

    @property
    def static_size(self) -> int:
        return WORD  # 全部基元静态占一个字（bytesN 也填一个字）


@dataclass(frozen=True)
class ArrayType(AbiType):
    element: AbiType
    length: int | None  # None => 动态数组
    depth: int = 1

    @property
    def is_dynamic(self) -> bool:
        return self.length is None or self.element.is_dynamic

    @property
    def static_size(self) -> int:
        if self.length is None:
            return WORD
        return self.length * self.element.static_size


@dataclass(frozen=True)
class TupleType(AbiType):
    components: tuple[AbiType, ...] = field(default_factory=tuple)

    @property
    def is_dynamic(self) -> bool:
        return any(c.is_dynamic for c in self.components)

    @property
    def static_size(self) -> int:
        return sum(c.static_size for c in self.components)


def _split_top_level(s: str) -> list[str]:
    """按逗号切分元组组件，忽略括号内的逗号。"""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise InvalidTypeError(f"括号不匹配: {s!r}")
        elif ch == "," and depth == 0:
            parts.append(s[start:i])
            start = i + 1
    if depth != 0:
        raise InvalidTypeError(f"括号不匹配: {s!r}")
    parts.append(s[start:])
    return parts


def _strip_array_suffix(s: str) -> tuple[str, tuple[int | None, ...]]:
    """剥离尾部所有 [k] / []，返回 (内层类型串, 维度列表，外->内顺序)。"""
    dims: list[int | None] = []
    while s.endswith("]"):
        open_idx = s.rfind("[")
        if open_idx == -1:
            raise InvalidTypeError(f"数组后缀缺少 '[': {s!r}")
        inner = s[open_idx + 1 : -1]
        if inner == "":
            dims.append(None)
        else:
            if not inner.isdigit():
                raise InvalidTypeError(f"数组长度必须是非负整数: {s!r}")
            k = int(inner)
            if k < 1:
                raise InvalidTypeError(f"定长数组长度必须 >= 1: {s!r}")
            dims.append(k)
        s = s[:open_idx]
    return s, tuple(dims)  # 从外到内


def parse_type(raw: str, *, depth: int = 0, _lenient: bool = False) -> AbiType:
    """把规范类型字符串解析为类型树。

    规范形式不允许空格；空元组、非法位宽、不支持基元都会抛错。
    depth 用于嵌套深度保护（含数组与元组的复合深度）。
    """
    if not isinstance(raw, str):
        raise InvalidTypeError(f"类型必须是字符串，得到 {type(raw).__name__}")
    if depth > MAX_DEPTH:
        from .errors import DepthLimitError

        raise DepthLimitError(f"类型嵌套超过 {MAX_DEPTH} 层")
    s = raw.strip()
    if s != raw and not _lenient:
        raise InvalidTypeError(f"规范类型串不允许空格: {raw!r}")
    if not s:
        raise InvalidTypeError("空类型串")

    # 数组后缀（可能多维）
    base_str, dims = _strip_array_suffix(s)
    if dims:
        t = parse_type(base_str, depth=depth + len(dims), _lenient=_lenient)
        # 从最内层向外包
        for k in reversed(dims):
            t = ArrayType(
                name=(f"{t.name}[{k if k is not None else ''}]"),
                element=t,
                length=k,
                depth=depth + 1,
            )
        return t

    # 元组
    if s.startswith("("):
        if not s.endswith(")"):
            raise InvalidTypeError(f"元组缺少右括号: {s!r}")
        inner = s[1:-1]
        if inner.strip() == "":
            raise InvalidTypeError("不允许空元组")
        comp_strs = _split_top_level(inner)
        comps = tuple(parse_type(c, depth=depth + 1, _lenient=_lenient) for c in comp_strs)
        return TupleType(name="(" + ",".join(c.name for c in comps) + ")", components=comps)

    # 基元
    return _parse_elementary(s, _lenient=_lenient)


def _parse_elementary(s: str, *, _lenient: bool) -> ElementaryType:
    if s in _SUPPORTED_DYNAMIC_ELEMENTARY:
        return ElementaryType(name=s, kind=s)

    m = _INT_RE.match(s)
    if m:
        signed = m.group(1) == "int"
        bits = int(m.group(2))
        if bits < 8 or bits > 256 or bits % 8 != 0:
            raise InvalidTypeError(f"非法位宽 {bits}: {s}（须为 8..256 的 8 的倍数）")
        return ElementaryType(name=s, kind="int" if signed else "uint", bits=bits, signed=signed)

    m = _BYTESN_RE.match(s)
    if m:
        n = int(m.group(1))
        if n < 1 or n > 32:
            raise InvalidTypeError(f"bytesN 的 N 须在 1..32: {s}")
        return ElementaryType(name=s, kind="bytesN", bits=n)

    # 裸 int/uint 等价 256；裸 bytes 已在上面处理
    if s in ("int", "uint"):
        return parse_type(s + "256", _lenient=_lenient)  # type: ignore[return-value]

    if _lenient and s in _SELECTOR_EXTRA_ELEMENTARY:
        # 只用于函数选择器：交给解析器记录，编解码主路径仍不支持
        return ElementaryType(name=s, kind=s)

    raise UnsupportedTypeError(f"不支持的类型: {s!r}（本后端支持 int/uint/bytes/string/数组/元组）")
