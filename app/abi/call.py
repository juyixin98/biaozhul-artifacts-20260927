"""函数选择器与 ABI 调用编解码。

选择器 = keccak256("name(canonicalTypes)") 的前 4 字节，
由成熟 Keccak 库生成（见 hashing.py）。

注意：选择器只对**类型签名**取哈希，签名中允许出现本后端编解码
不支持的 address/bool（它们仍有标准 canonical 名）；但 encode_call /
decode_call 处理参数时仍只接受受限类型，地址类参数请用 bytes20 传递。
"""

from __future__ import annotations

import re

from .codec import decode, encode
from .errors import InvalidTypeError, UnsupportedTypeError
from .hashing import keccak256
from .types import parse_type

_SIG_RE = re.compile(r"^([A-Za-z_$][A-Za-z0-9_$]*)\((.*)\)$", re.DOTALL)


def _canonical_signature(signature: str) -> tuple[str, str, tuple[str, ...]]:
    """解析 'name(t1,t2)'，返回 (name, 完整规范签名, 规范类型元组)。"""
    if not isinstance(signature, str):
        raise InvalidTypeError("签名必须是字符串")
    s = signature.strip()
    m = _SIG_RE.match(s)
    if not m:
        raise InvalidTypeError(f"函数签名格式错误: {signature!r}")
    name, args_str = m.group(1), m.group(2).strip()
    arg_src: list[str] = []
    if args_str:
        depth = 0
        start = 0
        for i, ch in enumerate(args_str):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                arg_src.append(args_str[start:i])
                start = i + 1
        arg_src.append(args_str[start:])
    canon_types: list[str] = []
    for raw in arg_src:
        token = raw.strip()
        # 仅选择器场景放宽 address/bool；其余受限类型规则不变
        t = parse_type(token, _lenient=True)
        canon_types.append(t.name)
    canon_sig = f"{name}({','.join(canon_types)})"
    return name, canon_sig, tuple(canon_types)


def function_selector(signature: str) -> bytes:
    """返回 4 字节函数选择器。"""
    _, canon_sig, _ = _canonical_signature(signature)
    return keccak256(canon_sig.encode("utf-8"))[:4]


def selector_for(signature: str) -> str:
    """便捷：返回 0x 前缀的 8 位十六进制选择器。"""
    return "0x" + function_selector(signature).hex()


def encode_call(signature: str, args) -> bytes:
    """编码 calldata = selector || abi.encode(args)。"""
    _, _, canon_types = _canonical_signature(signature)
    if len(args) != len(canon_types):
        raise InvalidTypeError(
            f"参数个数 {len(args)} 与签名参数 {len(canon_types)} 不符"
        )
    # 参数编解码走严格受限路径（不允许 address/bool）
    body = encode(list(canon_types), args)
    return function_selector(signature) + body


def decode_call(data: bytes, registry: dict[bytes, tuple[str, tuple[str, ...]]]):
    """按 {selector: (signature, types)} 注册表解码 calldata。

    返回 (signature, tuple_of_values)。未知选择器/截断输入抛
    InvalidTypeError / ABIDecodeError，绝不静默返回成功。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise InvalidTypeError("calldata 必须是 bytes")
    data = bytes(data)
    if len(data) < 4:
        raise InvalidTypeError(f"calldata 短于 4 字节选择器（len={len(data)}）")
    sel, body = data[:4], data[4:]
    entry = registry.get(sel)
    if entry is None:
        raise UnsupportedTypeError(f"未知函数选择器 0x{sel.hex()}")
    signature, types = entry
    values = decode(list(types), body)
    return signature, values
