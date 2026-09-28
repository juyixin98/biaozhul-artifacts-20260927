"""IP 字面量规范化。

策略判断前必须把所有等价写法收敛成同一个规范形式，否则
``::ffff:127.0.0.1``、``2130706433``、``0x7f.1`` 等会绕过只匹配
点分十进制的规则。

设计要点
========
* 基于标准库 :mod:`ipaddress` 做权威解析，自己只负责"输入清洗 + 等价形收敛"。
* IPv4-mapped IPv6 (``::ffff:a.b.c.d``) **解包**为 IPv4；同时把映射段
  规则（如 ``::ffff:0:0/96``）也纳入策略覆盖，做到解包前后都能命中。
* 宽松十进制形式（``0177.0.0.1``、``0x7f000001``、``2130706433``）按
  操作系统 ``inet_aton`` 语义识别并规范化——不是为了支持它们，而是为了
  在规范化后用同一套规则判掉，并在证据里标注原始形式。
"""

from __future__ import annotations

import ipaddress
import re

from ..errors import InputError

_HEX_RE = re.compile(r"^0[xX][0-9a-fA-F]+$")


def _parse_loose_ipv4(text: str) -> str | None:
    """复刻 inet_aton 的 a / a.b / a.b.c / a.b.c.d 与各段 8/16/24 位拼接语义。

    返回规范化点分十进制；不是宽松 IPv4 形态时返回 None。
    段允许 0x 十六进制、前导 0 八进制、纯十进制；单段可为 32 位整数。
    """

    if ":" in text or "%" in text:
        return None
    parts = text.split(".")
    if not 1 <= len(parts) <= 4:
        return None

    values: list[int] = []
    for i, part in enumerate(parts):
        if part == "":
            return None
        try:
            if part[:2].lower() == "0x":
                if not _HEX_RE.match(part):
                    return None
                v = int(part, 16)
            elif len(part) > 1 and part[0] == "0":
                # 前导 0：inet_aton 按八进制解释（Python3 纯 int 会按十进制）
                v = int(part, 8)
            else:
                v = int(part, 10)
        except ValueError:
            return None
        # 最后一段吸收剩余位
        width = {1: 32, 2: 8 if i == 0 else 24, 3: 8 if i < 2 else 16, 4: 8}[len(parts)]
        if v < 0 or v >> width:
            return None
        values.append(v)

    if len(parts) == 1:
        packed = values[0]
    elif len(parts) == 2:
        packed = (values[0] << 24) | values[1]
    elif len(parts) == 3:
        packed = (values[0] << 24) | (values[1] << 16) | values[2]
    else:
        packed = (values[0] << 24) | (values[1] << 16) | (values[2] << 8) | values[3]
    return str(ipaddress.IPv4Address(packed))


def canonicalize_ip_literal(text: str) -> tuple[str, int, str]:
    """把任意 IP 字面量规范化为 ``(规范字符串, AF_FAMILY, 形态说明)``。

    family 取 :mod:`socket` 的 AF_INET=2 / AF_INET6=10，避免上层 import socket
    的顺序耦合。

    顺序：**先严格标准形式，再宽松等价形式**。标准点分十进制不应被标成
    "宽松"；两者都失败则抛 :class:`InputError`（看起来像 IP 却无法解析，
    必须拒绝而不是退回域名解析）。
    """

    raw = text.strip()
    candidate = raw[1:-1] if raw.startswith("[") and raw.endswith("]") else raw

    # 1) 严格标准形式
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        addr = None
    if addr is not None:
        if isinstance(addr, ipaddress.IPv4Address):
            return str(addr), 2, "ipv4"
        if addr.ipv4_mapped is not None:
            return str(addr.ipv4_mapped), 2, "ipv4_mapped_ipv6"
        return str(addr), 10, "ipv6"

    # 2) 宽松等价形式（inet_aton 语义）
    if "[" not in raw and "%" not in raw:
        loose = _parse_loose_ipv4(raw)
        if loose is not None:
            return loose, 2, "loose_ipv4"

    # 3) 都不是：明确拒绝
    raise InputError(
        f"无法解析的 IP 字面量: {raw!r}",
        code="E_HOST_NOT_IP",
        details={"host": raw},
    )


def is_ip_literal(text: str) -> bool:
    """启发式：该主机是不是 IP 字面量（含括号 v6、纯整数、进制、点分数字）。"""

    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        return True
    if ":" in t:
        return True
    if _HEX_RE.match(t):                       # 0x7f000001
        return True
    if t.isdigit():                            # 2130706433
        return True
    # 点分且每段都是数字/进制写法（0177.0.0.1、127.0.0.1）
    if "." in t and all(
        seg.isdigit() or _HEX_RE.match(seg) for seg in t.split(".") if seg
    ):
        return True
    return False
