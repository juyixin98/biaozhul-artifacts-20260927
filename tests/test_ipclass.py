"""IP 分类事实来源测试 —— 期望值手写，与实现无关。"""
from __future__ import annotations

import pytest

from app.ipclass import classify_literal, is_internal

# (字面量, 期望 canonical, family, 必须带有的标签, 是否 internal)
EXPECTED = [
    ("127.0.0.1", "127.0.0.1", "ipv4", ["loopback"], True),
    ("127.1.2.3", "127.1.2.3", "ipv4", ["loopback"], True),
    ("127.255.255.254", "127.255.255.254", "ipv4", ["loopback"], True),
    ("0.0.0.0", "0.0.0.0", "ipv4", ["unspecified"], True),
    ("0.1.2.3", "0.1.2.3", "ipv4", ["unspecified"], True),
    ("10.0.0.1", "10.0.0.1", "ipv4", ["private"], True),
    ("172.16.5.5", "172.16.5.5", "ipv4", ["private"], True),
    ("172.31.255.255", "172.31.255.255", "ipv4", ["private"], True),
    ("172.32.0.1", "172.32.0.1", "ipv4", [], False),
    ("192.168.9.9", "192.168.9.9", "ipv4", ["private"], True),
    ("100.64.0.1", "100.64.0.1", "ipv4", ["cgnat"], True),
    ("100.127.255.255", "100.127.255.255", "ipv4", ["cgnat"], True),
    ("169.254.169.254", "169.254.169.254", "ipv4", ["link-local"], True),
    ("169.254.170.2", "169.254.170.2", "ipv4", ["link-local"], True),
    ("224.0.0.1", "224.0.0.1", "ipv4", ["multicast"], True),
    ("240.0.0.1", "240.0.0.1", "ipv4", ["reserved"], True),
    ("255.255.255.255", "255.255.255.255", "ipv4", ["reserved", "broadcast"], True),
    ("192.0.2.7", "192.0.2.7", "ipv4", ["documentation"], True),
    ("198.51.100.7", "198.51.100.7", "ipv4", ["documentation"], True),
    ("203.0.113.10", "203.0.113.10", "ipv4", ["documentation"], True),
    ("198.18.0.1", "198.18.0.1", "ipv4", ["benchmark"], True),
    ("198.19.255.255", "198.19.255.255", "ipv4", ["benchmark"], True),
    ("192.88.99.1", "192.88.99.1", "ipv4", ["special-service"], True),
    ("8.8.8.8", "8.8.8.8", "ipv4", [], False),
    ("1.1.1.1", "1.1.1.1", "ipv4", [], False),
    # IPv6
    ("::1", "::1", "ipv6", ["loopback"], True),
    ("::", "::", "ipv6", ["unspecified"], True),
    ("fe80::1", "fe80::1", "ipv6", ["link-local"], True),
    ("fc00::1", "fc00::1", "ipv6", ["unique-local"], True),
    ("fd12::abcd", "fd12::abcd", "ipv6", ["unique-local"], True),
    ("ff02::1", "ff02::1", "ipv6", ["multicast"], True),
    ("2001:db8::1", "2001:db8::1", "ipv6", ["documentation"], True),
    ("2002::1", "2002::1", "ipv6", ["6to4"], True),
    ("100::1", "100::1", "ipv6", ["discard-prefix"], True),
    ("2606:4700:4700::1111", "2606:4700:4700::1111", "ipv6", [], False),
]


@pytest.mark.parametrize("literal,canon,family,tags,internal", EXPECTED)
def test_classification_table(literal, canon, family, tags, internal):
    r = classify_literal(literal)
    assert r.canonical_ip == canon, literal
    assert r.family == family, literal
    for t in tags:
        assert t in r.tags, (literal, t, r.tags)
    assert is_internal(r.tags) is internal, literal


@pytest.mark.parametrize("literal,v4,tag", [
    ("::ffff:127.0.0.1", "127.0.0.1", "loopback"),
    ("::ffff:169.254.169.254", "169.254.169.254", "link-local"),
    ("::ffff:10.0.0.1", "10.0.0.1", "private"),
    ("::ffff:8.8.8.8", "8.8.8.8", None),
    ("::ffff:203.0.113.10", "203.0.113.10", "documentation"),
    ("::ffff:0:0", "0.0.0.0", "unspecified"),
])
def test_mapped_ipv6_unwrapped_to_ipv4(literal, v4, tag):
    r = classify_literal(literal)
    assert r.family == "ipv4", literal
    assert r.canonical_ip == v4, literal
    assert r.unwrapped_from is not None, literal
    if tag:
        assert tag in r.tags, (literal, r.tags)


def test_ipv4_compatible_unwrapped_but_loopback_compressed_stays_v6():
    # ::1 是 IPv6 loopback，不能被解包成 0.0.0.1
    r = classify_literal("::1")
    assert r.family == "ipv6"
    assert r.unwrapped_from is None


def test_illegal_literals_raise():
    for bad in ["not-an-ip", "999.1.1.1", "::ffff:999.0.0.1", "1.2.3", "gg::"]:
        with pytest.raises(ValueError):
            classify_literal(bad)
