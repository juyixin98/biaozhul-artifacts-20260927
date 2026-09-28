"""规则 / 证据解析层。

职责（只做声明式解析，不做隐私判断）：
- HTTP 头部名归一化；
- 区分响应 Vary 的三种状态：缺失(absent) / 通配(wildcard) / 显式列表(explicit)；
- 解析 Cache-Control 与默认可缓存状态码；
- query 串的确定性规范化；
- 响应体 SHA-256 校验（不符则 computation_failure）。

注意：本模块不“理解业务隐私”，只按 HTTP 声明规则工作。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from typing import Iterable
from urllib.parse import parse_qsl, urlencode

from .errors import ComputationFailureError, ErrorCode, InputError

# RFC 9111 默认可缓存的状态码（无显式缓存指令时）
DEFAULT_CACHEABLE_STATUS = {200, 203, 204, 206, 301, 404, 405, 410, 414, 501}

VARY_ABSENT = "absent"
VARY_WILDCARD = "wildcard"
VARY_EXPLICIT = "explicit"


def normalize_header_name(name: str) -> str:
    """头部名归一：去空白、Title-Case（如 accept-language → Accept-Language）。"""
    n = name.strip()
    if not n:
        raise ValueError("头部名不能为空")
    if any(ch.isspace() for ch in n.replace(" ", "")[0:0]):  # pragma: no cover - 占位
        pass
    parts = n.split("-")
    return "-".join(p[:1].upper() + p[1:].lower() for p in parts if p)


def _header_value(headers: dict[str, str], name: str) -> str | None:
    return headers.get(name) or headers.get(normalize_header_name(name))


def parse_vary(headers: dict[str, str]) -> tuple[str, list[str]]:
    """解析响应 Vary。

    返回 (state, names)：
      ("absent",   [])         —— 完全没有 Vary 头；
      ("wildcard", [])         —— Vary: *（语义：每个请求都不同，禁止重用）；
      ("explicit", [..names..])—— 显式列出的请求头（归一化、去重保序）。
    通配与显式混写（``Vary: *, Accept-Language``）按 RFC 视为通配。
    """
    raw = _header_value(headers, "Vary")
    if raw is None:
        return VARY_ABSENT, []
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return VARY_ABSENT, []
    if any(t == "*" for t in tokens):
        return VARY_WILDCARD, []
    names: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        n = normalize_header_name(t)
        if n not in seen:
            seen.add(n)
            names.append(n)
    return VARY_EXPLICIT, names


def parse_cache_control(headers: dict[str, str]) -> dict[str, str | bool]:
    """解析 Cache-Control：无值指令记 True，带值指令记字符串值。"""
    raw = _header_value(headers, "Cache-Control")
    out: dict[str, str | bool] = {}
    if not raw:
        return out
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip().lower()] = v.strip().strip('"')
        else:
            out[item.lower()] = True
    return out


def canonical_query(query: str) -> str:
    """query 规范化：按 (key,value) 排序，顺序无关，重复键保留。"""
    pairs = parse_qsl(query, keep_blank_values=True)
    return urlencode(sorted(pairs), doseq=True)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def decode_body(body: str, encoding: str) -> bytes:
    """按编码取出响应体原始字节；base64 非法属于输入错误。"""
    if encoding == "utf-8":
        return body.encode("utf-8")
    try:
        return base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InputError(
            "body_encoding=base64 但 body 不是合法 base64",
            code=ErrorCode.EVIDENCE_INVALID,
        ) from exc


def verify_body_hash(declared_sha256: str, body: str, encoding: str = "utf-8") -> None:
    """核对响应体摘要；不符抛 computation_failure(BODY_HASH_MISMATCH)。"""
    actual = sha256_hex(decode_body(body, encoding))
    if actual != declared_sha256.strip().lower():
        raise ComputationFailureError(
            "响应体 SHA-256 与声明不符，证据可能被截断或篡改",
            code=ErrorCode.BODY_HASH_MISMATCH,
            details={"declared": declared_sha256, "actual": actual},
        )


def is_response_cacheable(status: int, headers: dict[str, str]) -> bool:
    """按 HTTP 声明判断该响应是否允许进入缓存（不涉及该不该共享给某身份）。"""
    cc = parse_cache_control(headers)
    if cc.get("no-store"):
        return False
    if "max-age" in cc or "s-maxage" in cc or cc.get("public"):
        return status < 400 or status in DEFAULT_CACHEABLE_STATUS
    if cc.get("private") is True or cc.get("no-cache") is True:
        # private/no-cache 仍可存储（private 是私有存储），交给内核按作用域判
        return status in DEFAULT_CACHEABLE_STATUS or status < 400
    return status in DEFAULT_CACHEABLE_STATUS


def fingerprint_headers(headers: dict[str, str], names: Iterable[str]) -> str:
    """对选定头部做确定性指纹（用于 Vary:* 兜底键与中间状态展示）。"""
    parts = []
    for n in names:
        key = normalize_header_name(n)
        val = headers.get(key, "")
        parts.append(f"{key}={val.strip()}")
    return sha256_hex("\n".join(parts))


def witness_id(evidence_a: str, evidence_b: str, dimension: str) -> str:
    lo, hi = sorted([evidence_a, evidence_b])
    return "w_" + sha256_hex(f"{lo}|{hi}|{dimension}")[:16]
