"""规则/证据解析：Vary 通配与缺失必须可区分；摘要与缓存指令解析。"""
from __future__ import annotations

import pytest

from app.errors import ComputationFailureError, InputError
from app.parser import (
    VARY_ABSENT,
    VARY_EXPLICIT,
    VARY_WILDCARD,
    canonical_query,
    decode_body,
    is_response_cacheable,
    normalize_header_name,
    parse_cache_control,
    parse_vary,
    sha256_hex,
    verify_body_hash,
)


def test_normalize_header_name():
    assert normalize_header_name("accept-language") == "Accept-Language"
    assert normalize_header_name(" AUTHORIZATION ") == "Authorization"


def test_vary_states_are_distinct():
    assert parse_vary({}) == (VARY_ABSENT, [])
    assert parse_vary({"Vary": ""}) == (VARY_ABSENT, [])
    assert parse_vary({"Vary": "*"}) == (VARY_WILDCARD, [])
    assert parse_vary({"Vary": "*, Accept-Language"}) == (VARY_WILDCARD, [])
    state, names = parse_vary({"Vary": "accept-language, accept-encoding, accept-language"})
    assert state == VARY_EXPLICIT
    assert names == ["Accept-Language", "Accept-Encoding"]  # 归一化且去重保序


def test_cache_control_and_cacheability():
    assert is_response_cacheable(200, {"Cache-Control": "max-age=10"})
    assert not is_response_cacheable(200, {"Cache-Control": "no-store"})
    assert is_response_cacheable(404, {})  # 404 默认可缓存
    assert not is_response_cacheable(500, {})
    cc = parse_cache_control({"Cache-Control": 'private, max-age="0", no-cache'})
    assert cc["private"] is True
    assert cc["max-age"] == "0"
    assert cc["no-cache"] is True


def test_canonical_query_order_independent():
    assert canonical_query("b=2&a=1") == canonical_query("a=1&b=2")
    assert canonical_query("x=1&x=2") == "x=1&x=2"


def test_body_hash_ok_and_mismatch_is_computation_failure():
    body = "abc"
    good = sha256_hex(body)
    verify_body_hash(good, body)  # 不抛
    with pytest.raises(ComputationFailureError) as ei:
        verify_body_hash("0" * 64, body)
    assert ei.value.code == "body_hash_mismatch"
    assert ei.value.category == "computation_failure"
    assert ei.value.details["actual"] == good


def test_decode_body_base64():
    raw = b"\x00\x01\xff"
    import base64
    b64 = base64.b64encode(raw).decode()
    assert decode_body(b64, "base64") == raw
    with pytest.raises(InputError):
        decode_body("not-base64!!!", "base64")
