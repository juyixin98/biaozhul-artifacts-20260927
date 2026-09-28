"""受控 URL 解析与主机规范化。

设计原则（与 DNS、连接使用同一套策略）：

* 只允许 ``http`` / ``https``；
* 拒绝 userinfo（``https://user@host/``）——用户名片段是经典混淆面；
* 显式识别整数 IP / 多进制 / 多段数字等非点分十进制写法，避免绕过分类；
* IPv4-mapped IPv6 字面量在此标记，分类时解包；
* IDNA 主机名在此转 ASCII，后续 DNS 只看到规范化结果；
* 规范化 URL 带显式端口、去 fragment，作为重定向环检测的稳定键。

该模块不发起任何网络请求。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .contracts import InputError, ParsedTarget, Reason

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}

# authority 中直接拒绝的字符：反斜杠、空白、控制字符、# 与 ?（查询必须在 host 之后）
_BAD_AUTHORITY_CHARS = re.compile(r"[\\\s#?]|[\x00-\x1f\x7f]")
# 非点分形式的纯数字/进制数字（0x、0o、0b 前缀或全数字）
_INTEGER_IP_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|0[oO][0-7]+|0[bB][01]+|\d+)$")
# 点分十进制四段
_DOTTED_QUAD_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
# 其余“数字.数字...”混合（八进制/十六进制段等歧义写法）
_NUMERIC_SEGMENTS_RE = re.compile(r"^[\dxXa-fA-FoObB.]+$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def parse_target(raw_url: str) -> ParsedTarget:
    """解析并规范化一个出站 URL。

    :raises InputError: URL 形态非法（原因码见 :class:`Reason`）
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise InputError(Reason.URL_MALFORMED, "URL 为空或非字符串", {"input": _redact(raw_url)})
    url = raw_url.strip()

    try:
        parts = urlsplit(url)
    except ValueError as exc:  # IPv6 方括号错误等
        raise InputError(Reason.URL_MALFORMED, f"URL 无法解析: {exc}", {"input": _redact(url)}) from exc

    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InputError(
            Reason.SCHEME_UNSUPPORTED,
            f"仅允许 http/https，收到 {scheme!r}",
            {"scheme": scheme},
        )

    netloc = parts.netloc
    if not netloc:
        raise InputError(Reason.HOST_MISSING, "URL 缺少主机（authority）", {"input": _redact(url)})
    if _BAD_AUTHORITY_CHARS.search(netloc):
        raise InputError(
            Reason.URL_BAD_CHARACTER,
            "authority 含反斜杠/空白/控制字符/片段定界符",
            {"netloc": _redact(netloc)},
        )

    has_userinfo = "@" in netloc
    if has_userinfo:
        # 不剥离 userinfo 后继续 —— 用户名片段一律禁止（含“user@1.2.3.4”混淆）
        raise InputError(
            Reason.USERINFO_FORBIDDEN,
            "URL 中禁止出现用户名/密码片段（userinfo）",
            {"userinfo": _redact(netloc.rsplit("@", 1)[0])},
        )

    try:
        explicit_port = parts.port
    except ValueError as exc:  # 端口非数字或越界
        raise InputError(
            Reason.PORT_INVALID,
            f"端口非法或越界: {exc}",
            {"netloc": _redact(netloc)},
        ) from exc
    port = explicit_port if explicit_port is not None else DEFAULT_PORTS[scheme]

    host_raw = parts.hostname or ""
    if not host_raw:
        raise InputError(Reason.HOST_MISSING, "URL 缺少主机名", {"input": _redact(url)})

    host, host_kind, lookup_host = _normalize_host(host_raw)
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    # 查询保留原文；fragment 一律丢弃（不参与环检测，也不发送）
    canonical = f"{scheme}://{_authority(host, host_kind, port)}{path}"
    if parts.query:
        canonical += "?" + parts.query

    return ParsedTarget(
        url=canonical,
        scheme=scheme,
        host=host,
        host_kind=host_kind,
        port=port,
        has_userinfo=has_userinfo,
        path=path,
        raw_host=host_raw,
        normalized_for_lookup=lookup_host,
    )


def join_redirect(base: ParsedTarget, location: str) -> str:
    """把相对 ``Location`` 解析为绝对 URL；不做策略判断（内核做）。"""
    from urllib.parse import urljoin

    if not location or not location.strip():
        raise InputError(
            Reason.REDIRECT_LOCATION_MISSING,
            "重定向响应缺少 Location",
            {"base": base.url},
        )
    joined = urljoin(base.url, location.strip())
    return joined


def _authority(host: str, kind: str, port: int) -> str:
    if kind == "ipv6":
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def _normalize_host(raw: str) -> tuple[str, str, str]:
    """返回 (规范化主机, host_kind, 供 lookup/分类的形态)。"""
    # 1) IPv6 字面量（urlsplit.hostname 已去方括号；裸 ::1 也识别）
    if ":" in raw and _looks_like_ipv6(raw):
        return _normalize_ipv6(raw)

    # 2) 点分十进制 IPv4
    if _DOTTED_QUAD_RE.match(raw):
        return _normalize_dotted_quad(raw)

    # 3) 纯整数 / 多进制单段 —— 拒绝（经典整数 IP 绕过）
    if _INTEGER_IP_RE.match(raw):
        raise InputError(
            Reason.HOST_INTEGER_IP,
            "禁止使用整数/多进制形式的 IP 主机名",
            {"raw_host": raw},
        )

    # 4) 其他“数字段.数字段”写法（八进制段、1.2 短形式等歧义编码）
    if _NUMERIC_SEGMENTS_RE.match(raw) and any(ch.isdigit() for ch in raw) and "." in raw:
        raise InputError(
            Reason.HOST_AMBIGUOUS_NUMERIC,
            "禁止使用非标准点分十进制的数字主机名（八进制/短形式等）",
            {"raw_host": raw},
        )

    # 5) DNS 名：IDNA → ASCII、尾点、标签校验
    return _normalize_dns(raw)


def _looks_like_ipv6(raw: str) -> bool:
    # urlsplit 对 [::1] 给出 hostname='::1'；裸 ::1 也认作 IPv6 字面量
    import ipaddress

    try:
        ipaddress.IPv6Address(raw)
        return True
    except ValueError:
        return False


def _normalize_ipv6(raw: str) -> tuple[str, str, str]:
    import ipaddress

    try:
        addr = ipaddress.IPv6Address(raw)
    except ValueError as exc:
        raise InputError(Reason.IP_MALFORMED, f"非法 IPv6 字面量: {raw}", {"raw_host": raw}) from exc
    compressed = addr.compressed
    return compressed, "ipv6", compressed


def _normalize_dotted_quad(raw: str) -> tuple[str, str, str]:
    import ipaddress

    parts = [int(p) for p in raw.split(".")]
    if any(not 0 <= p <= 255 for p in parts):
        raise InputError(Reason.IP_MALFORMED, "点分十进制段超出 0-255", {"raw_host": raw})
    addr = ipaddress.IPv4Address(bytes(parts))
    canon = str(addr)
    return canon, "ipv4", canon


def _normalize_dns(raw: str) -> tuple[str, str, str]:
    name = raw
    if name.endswith("."):
        name = name[:-1]
    if not name:
        raise InputError(Reason.HOST_MISSING, "主机名仅为根标签", {"raw_host": raw})
    labels = name.split(".")
    ascii_labels: list[str] = []
    try:
        for label in labels:
            if not label:
                raise InputError(Reason.HOST_INVALID_LABEL, "主机名含空标签", {"raw_host": raw})
            if label.isascii():
                if not _LABEL_RE.match(label.lower()):
                    raise InputError(
                        Reason.HOST_INVALID_LABEL,
                        f"DNS 标签非法: {label!r}",
                        {"raw_host": raw, "label": label},
                    )
                ascii_labels.append(label.lower())
            else:
                encoded = label.encode("idna").decode("ascii")
                if not _LABEL_RE.match(encoded):
                    raise InputError(
                        Reason.HOST_INVALID_LABEL,
                        f"IDNA 编码后标签非法: {label!r}",
                        {"raw_host": raw},
                    )
                ascii_labels.append(encoded)
    except UnicodeError as exc:
        raise InputError(
            Reason.HOST_INVALID_LABEL,
            f"IDNA 编码失败: {exc}",
            {"raw_host": raw},
        ) from exc

    ascii_name = ".".join(ascii_labels)
    if len(ascii_name) > 253:
        raise InputError(Reason.HOST_INVALID_LABEL, "规范化主机名超过 253 字符", {"host": ascii_name})
    return ascii_name, "dns", ascii_name


def _redact(value: str | None) -> str:
    """日志里只保留形态，不保留可能含凭据的长原文。"""
    if value is None:
        return ""
    if len(value) <= 32:
        return value
    return value[:29] + "..."
