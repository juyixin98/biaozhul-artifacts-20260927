"""IP 字面量识别与地址段分类。

这是“该地址是否危险”的**事实来源**：策略层只负责把标签映射为 allow/deny。
使用显式网段集合而不是仅依赖 ``is_private`` 等属性，以保证各 Python 版本
行为一致、且 0.0.0.0/8、CGNAT、benchmark 等“看起来公网、实则特殊”的段不遗漏。

关键规则：

* IPv4-mapped IPv6（``::ffff:1.2.3.4``）与 IPv4-compatible（``::1.2.3.4``）
  解包为 IPv4 后按 IPv4 段分类 —— 否则 ``http://[::ffff:169.254.169.254]/``
  这类写法可以绕过只检查 IPv4 的实现；
* 解包后的连接族也变为 AF_INET（由 connector 消费 ``ResolvedAddress``）。
"""
from __future__ import annotations

import ipaddress

from .contracts import ResolvedAddress

# --- IPv4：标签 -> 网段集合（显式声明，交叉标签保留全部） -----------------
_IPV4_NETS: list[tuple[str, tuple[str, ...]]] = [
    ("unspecified", ("0.0.0.0/8",)),
    ("loopback", ("127.0.0.0/8",)),
    # 169.254.0.0/16：link-local，云元数据服务 169.254.169.254 在其中
    ("link-local", ("169.254.0.0/16",)),
    ("private", ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")),
    ("cgnat", ("100.64.0.0/10",)),
    ("multicast", ("224.0.0.0/4",)),
    ("reserved", ("240.0.0.0/4",)),
    ("broadcast", ("255.255.255.255/32",)),
    ("documentation", ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")),
    ("benchmark", ("198.18.0.0/15",)),
    # 192.88.99.0/24 历史为 6to4 relay anycast（已废弃但仍被特殊对待）
    ("special-service", ("192.88.99.0/24",)),
]

# --- IPv6：标签 -> 网段集合 -----------------------------------------------
_IPV6_NETS: list[tuple[str, tuple[str, ...]]] = [
    ("loopback", ("::1/128",)),
    ("unspecified", ("::/128",)),
    ("ipv4-mapped", ("::ffff:0:0/96",)),
    ("ipv4-compatible", ("::/96",)),  # ::1.2.3.4（排除 ::/128 与 ::ffff: 由解包顺序处理）
    ("discard-prefix", ("100::/64",)),
    ("tail-router", ("2001::/32",)),
    ("documentation", ("2001:db8::/32",)),
    ("6to4", ("2002::/16",)),
    ("unique-local", ("fc00::/7",)),
    ("link-local", ("fe80::/10",)),
    ("multicast", ("ff00::/8",)),
    # Teredo 默认服务器等特殊用途（2001:0000::/32 即 2001::/32 已列；另列 teredo 段）
    ("teredo", ("2001::/32",)),
]

# 解包为 IPv4 后必须重新按 IPv4 分类的两类（顺序：loopback/unspecified 除外）
_MAPPPED_V6_NET = ipaddress.IPv6Network("::ffff:0:0/96")
_COMPAT_V6_NET = ipaddress.IPv6Network("::/96")

_IPV4_COMPILED = [
    (tag, tuple(ipaddress.IPv4Network(n) for n in nets)) for tag, nets in _IPV4_NETS
]
_IPV6_COMPILED = [
    (tag, tuple(ipaddress.IPv6Network(n) for n in nets)) for tag, nets in _IPV6_NETS
]

# 标记“默认应拒绝”的标签（policy 的 default-deny 规则引用此集合）
INTERNAL_TAGS = frozenset(
    {
        "unspecified",
        "loopback",
        "link-local",
        "private",
        "cgnat",
        "multicast",
        "reserved",
        "broadcast",
        "documentation",
        "benchmark",
        "special-service",
        "discard-prefix",
        "tail-router",
        "6to4",
        "unique-local",
        "teredo",
        # ipv4-mapped/ipv4-compatible 自身不直接进集合——它们已解包为 IPv4 标签
    }
)


def classify_literal(literal: str) -> ResolvedAddress:
    """把一个 IP 字面量分类为 :class:`ResolvedAddress`。

    对 mapped/compatible IPv6 返回解包后的 IPv4 候选（``unwrapped_from``
    保留原字面量）。``::1`` / ``::`` 保留 IPv6 语义（loopback/unspecified）。
    非法字面量抛 ``ValueError``。
    """
    addr = ipaddress.ip_address(literal)
    if isinstance(addr, ipaddress.IPv6Address):
        v6_tags = _ipv6_tags(addr)
        # loopback / unspecified 即便落在 ::/96 内也不解包（::1 是 IPv6 回环）
        if not (v6_tags & {"loopback", "unspecified"}):
            v4_int: int | None = None
            if addr in _MAPPPED_V6_NET:
                v4_int = int.from_bytes(addr.packed[12:], "big")
            elif addr in _COMPAT_V6_NET and int(addr) != 0:
                v4_int = int(addr)  # ::a.b.c.d，低 32 位即 IPv4
            if v4_int is not None:
                v4_addr = ipaddress.IPv4Address(v4_int)
                return ResolvedAddress(
                    ip=str(v4_addr),
                    family="ipv4",
                    canonical_ip=str(v4_addr),
                    tags=_ipv4_tags(v4_addr),
                    unwrapped_from=addr.compressed,
                )
        return ResolvedAddress(
            ip=addr.compressed,
            family="ipv6",
            canonical_ip=addr.compressed,
            tags=v6_tags,
        )
    tags = _ipv4_tags(addr)
    return ResolvedAddress(ip=str(addr), family="ipv4", canonical_ip=str(addr), tags=tags)


def _ipv4_tags(addr: ipaddress.IPv4Address) -> frozenset[str]:
    return frozenset(tag for tag, nets in _IPV4_COMPILED if any(addr in n for n in nets))


def _ipv6_tags(addr: ipaddress.IPv6Address) -> frozenset[str]:
    tags: set[str] = set()
    for tag, nets in _IPV6_COMPILED:
        if tag in ("ipv4-mapped", "ipv4-compatible"):
            continue  # 这两类在 classify_literal 已解包，不应作为最终标签
        if any(addr in n for n in nets):
            tags.add(tag)
    return frozenset(tags)


def is_internal(tags: frozenset[str]) -> bool:
    """候选地址是否携带任一“默认拒绝”标签。"""
    return bool(tags & INTERNAL_TAGS)
