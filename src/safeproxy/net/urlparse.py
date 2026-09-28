"""严格 URL 解析与主机规范化。

安全立场
========
Python/浏览器的 URL 解析器对很多畸形输入"宽容地接受"，而解析差异正是
SSRF 绕过的主要来源。这里采用**最小可解释面**：

* scheme 白名单外直接 INPUT_ERROR；
* 出现 userinfo（``//user@host``）默认判输入错误，绝不静默丢弃后放行；
* 主机中的百分号编码（``%31%32%37...``）拒绝，而不是解码——不同下游
  解码行为不一致；
* IPv6 区域 ID（``fe80::1%eth0``）拒绝（可用于接口枚举/解析差异）；
* 控制字符、空白、反斜杠、at 号等歧义字符拒绝；
* 默认端口显式补全，后续策略只面对具体 ``host:port``。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..contracts import HostKind, ParsedUrl
from ..errors import InputError
from .addrip import canonicalize_ip_literal, is_ip_literal

_SCHEME_PORTS = {"http": 80, "https": 443}
# 主机中绝不允许出现的字节：控制字符、空白、反斜杠、百分号（拒绝编码主机）
_HOST_FORBIDDEN = re.compile(r"[\x00-\x20\x7f\\%]")
# 可打印但在 authority 中具歧义、安全上不接受的字符
_AMBIGUOUS = set('<>^"`{}| ,')


def parse_and_normalize(url: str, allowed_schemes: frozenset[str]) -> ParsedUrl:
    if not isinstance(url, str) or not url:
        raise InputError("URL 必须是非空字符串", code="E_URL_EMPTY")

    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in url):
        raise InputError("URL 含控制字符", code="E_URL_CONTROL_CHAR", details={"url": _redact(url)})

    try:
        parts = urlsplit(url.strip())
    except ValueError as exc:
        code = "E_IPV6_BRACKET" if "[" in url else "E_URL_UNSPLIT"
        raise InputError(f"URL 无法分割: {exc}", code=code, details={"url": _redact(url)}) from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in allowed_schemes:
        raise InputError(
            f"scheme {scheme!r} 不在允许集合 {sorted(allowed_schemes)} 内",
            code="E_SCHEME_FORBIDDEN",
            details={"scheme": scheme, "allowed": sorted(allowed_schemes)},
        )
    if scheme not in _SCHEME_PORTS:  # 白名单内容与已知端口表必须一致
        raise InputError(f"内部错误：scheme {scheme} 无默认端口表", code="E_SCHEME_NO_PORT")

    netloc = parts.netloc
    if not netloc:
        raise InputError("URL 缺少 authority（主机）", code="E_URL_NO_HOST", details={"url": _redact(url)})

    # 整个 authority 层面先拒绝百分号编码/反斜杠/控制字符（无论在 userinfo
    # 还是 host 中）——下游对此类输入解释不一致，安全上不允许进入规范化。
    if "%" in netloc or "\\" in netloc or any(
        ord(c) < 0x20 or ord(c) == 0x7f for c in netloc
    ):
        raise InputError(
            "authority 含百分号编码/反斜杠/控制字符",
            code="E_HOST_BAD_CHAR",
            details={"url": _redact(url)},
        )

    # ---- userinfo：显式标记，不静默剥离 ----
    witness = ""
    has_ui = "@" in netloc
    if has_ui:
        userinfo, _, hostpart = netloc.rpartition("@")
        witness = _mask_userinfo(userinfo)
        if "@" in hostpart:  # 多个 @ 一律拒
            raise InputError("authority 含多个 '@'", code="E_USERINFO_MULTI_AT",
                             details={"url": _redact(url)})
    else:
        hostpart = netloc

    # ---- 拆端口 ----
    host_raw, port = _split_port(hostpart, scheme)

    # ---- 主机字符集校验（在任何规范化之前拒绝歧义/编码）----
    _reject_ambiguous_host(host_raw)

    if not host_raw:
        raise InputError("主机为空", code="E_HOST_EMPTY")

    # ---- 规范化主机 ----
    if is_ip_literal(host_raw):
        canonical, _family, _shape = canonicalize_ip_literal(host_raw)
        kind = HostKind.IPV6 if ":" in canonical else HostKind.IPV4
        host_display = f"[{canonical}]" if ":" in canonical else canonical
    else:
        canonical, host_display = _normalize_domain(host_raw)
        kind = HostKind.DOMAIN

    if has_ui:
        # 保留证据但由策略层决定（默认 deny_userinfo=True → INPUT_ERROR）
        # 此处不直接抛，使策略可配置，同时决策链能记录该跳。
        pass

    path = parts.path or "/"
    return ParsedUrl(
        url=url,
        scheme=scheme,
        host=canonical,
        host_kind=kind,
        port=port,
        has_userinfo=has_ui,
        userinfo_witness=witness,
        path=path,
        query=parts.query or "",
        fragment=parts.fragment or "",
    )


def _split_port(hostpart: str, scheme: str) -> tuple[str, int]:
    # IPv6 字面量 [::1]:8080
    if hostpart.startswith("["):
        rb = hostpart.find("]")
        if rb == -1:
            raise InputError("IPv6 主机缺少 ']'", code="E_IPV6_BRACKET")
        host = hostpart[: rb + 1]
        tail = hostpart[rb + 1 :]
        if tail == "":
            return host, _SCHEME_PORTS[scheme]
        if not tail.startswith(":"):
            raise InputError("IPv6 括号后存在非法字符", code="E_HOST_BAD_TAIL", details={"tail": tail})
        return host, _parse_port(tail[1:])
    if hostpart.count(":") > 1:
        raise InputError("裸 IPv6 必须加方括号", code="E_IPV6_RAW", details={"host": hostpart})
    if ":" in hostpart:
        host, _, p = hostpart.rpartition(":")
        return host, _parse_port(p)
    return hostpart, _SCHEME_PORTS[scheme]


def _parse_port(text: str) -> int:
    if not text.isdigit():
        raise InputError(f"端口非数字: {text!r}", code="E_PORT_NONNUMERIC")
    port = int(text)
    if not 1 <= port <= 65535:
        raise InputError(f"端口越界: {port}", code="E_PORT_RANGE", details={"port": port})
    return port


def _reject_ambiguous_host(host: str) -> None:
    inner = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    if _HOST_FORBIDDEN.search(inner):
        raise InputError("主机含控制字符/空白/反斜杠/百分号编码", code="E_HOST_BAD_CHAR",
                         details={"host": _redact(inner)})
    bad = sorted({c for c in inner if c in _AMBIGUOUS})
    if bad:
        raise InputError(f"主机含歧义字符 {bad}", code="E_HOST_AMBIGUOUS",
                         details={"host": _redact(inner), "chars": bad})
    if "%" in inner:  # IPv6 scope id
        raise InputError("拒绝带区域 ID 的 IPv6 字面量", code="E_IPV6_SCOPE",
                         details={"host": _redact(inner)})


def _normalize_domain(host: str) -> tuple[str, str]:
    h = host.lower().strip(".")
    if not h:
        raise InputError("域名为空（仅点号）", code="E_HOST_EMPTY")
    # 标签校验：每个标签 1-63 字符，允许字母数字连字符；下划线宽松拒绝
    labels = h.split(".")
    for label in labels:
        if not 1 <= len(label) <= 63:
            raise InputError("DNS 标签长度越界", code="E_DNS_LABEL_LEN", details={"label": label})
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            raise InputError(f"DNS 标签非法: {label!r}", code="E_DNS_LABEL_CHAR",
                             details={"label": label})
    if len(h) > 253:
        raise InputError("域名超过 253 字符", code="E_DNS_NAME_LEN")
    return h, h


def join_redirect(base: ParsedUrl, location: str) -> str:
    """把 Location 头按 RFC3986 合并到当前 URL，返回下一跳绝对 URL 字符串。

    反斜杠等仍交给下一跳的严格解析器判掉；这里只负责标准合并。
    """

    from urllib.parse import urljoin

    if not location:
        raise InputError("重定向 Location 为空", code="E_REDIRECT_NO_LOCATION")
    base_url = f"{base.scheme}://{_authority(base)}{base.path}"
    if base.query:
        base_url += f"?{base.query}"
    nxt = urljoin(base_url, location.strip())
    return nxt


def _authority(p: ParsedUrl) -> str:
    h = f"[{p.host}]" if p.host_kind == HostKind.IPV6 else p.host
    default = _SCHEME_PORTS.get(p.scheme)
    if default is not None and p.port == default:
        return h
    return f"{h}:{p.port}"


def _mask_userinfo(ui: str) -> str:
    if not ui:
        return "@"
    head = ui[0]
    return f"{head}***@"


def _redact(s: str) -> str:
    return s if len(s) <= 32 else s[:29] + "..."
