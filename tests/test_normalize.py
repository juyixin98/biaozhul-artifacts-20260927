"""URL/IP 规范化单元测试（具体等价形式）。"""

from __future__ import annotations

import pytest

from safeproxy.errors import InputError
from safeproxy.net.addrip import canonicalize_ip_literal
from safeproxy.net.urlparse import parse_and_normalize

SCHEMES = frozenset({"http", "https"})


@pytest.mark.parametrize(
    "raw,expected,shape",
    [
        ("127.0.0.1", "127.0.0.1", "ipv4"),
        ("2130706433", "127.0.0.1", "loose_ipv4"),
        ("0x7f000001", "127.0.0.1", "loose_ipv4"),
        ("0177.0.0.1", "127.0.0.1", "loose_ipv4"),
        ("0x7f.1", "127.0.0.1", "loose_ipv4"),
        ("::ffff:127.0.0.1", "127.0.0.1", "ipv4_mapped_ipv6"),
        ("[::ffff:169.254.169.254]", "169.254.169.254", "ipv4_mapped_ipv6"),
        ("169.254.169.254", "169.254.169.254", "ipv4"),
        ("::1", "::1", "ipv6"),
        ("fe80::1", "fe80::1", "ipv6"),
    ],
)
def test_canonical_forms(raw, expected, shape):
    literal, family, got_shape = canonicalize_ip_literal(raw)
    assert literal == expected
    assert got_shape == shape


def test_bad_ip_literal_raises():
    with pytest.raises(InputError):
        canonicalize_ip_literal("999.999.999.999")


def test_userinfo_preserved_not_silently_dropped():
    p = parse_and_normalize("http://attacker@127.0.0.1:8080/", SCHEMES)
    assert p.has_userinfo is True
    assert p.host == "127.0.0.1"
    assert p.userinfo_witness.startswith("a")
    assert p.userinfo_witness.endswith("@")


def test_percent_encoded_host_rejected():
    with pytest.raises(InputError) as ei:
        parse_and_normalize("http://127.0.0.1%2f@evil/", SCHEMES)
    assert ei.value.code == "E_HOST_BAD_CHAR"


def test_default_port_filled():
    p = parse_and_normalize("http://example.com/path", SCHEMES)
    assert p.port == 80
    p2 = parse_and_normalize("https://example.com", SCHEMES)
    assert p2.port == 443


@pytest.mark.parametrize(
    "url,code",
    [
        ("gopher://x/", "E_SCHEME_FORBIDDEN"),
        ("http://", "E_URL_NO_HOST"),
        ("http://x:70000/", "E_PORT_RANGE"),
        ("http://x:abc/", "E_PORT_NONNUMERIC"),
        ("http://[::1/", "E_IPV6_BRACKET"),
        ("http://::1/", "E_IPV6_RAW"),
        ("", "E_URL_EMPTY"),
        ("http://exa mple.com/", "E_HOST_BAD_CHAR"),
        ("http://exa\\mple.com/", "E_HOST_BAD_CHAR"),
        ("http://fe80::1%25eth0/", "E_HOST_BAD_CHAR"),
    ],
)
def test_malformed_inputs(url, code):
    with pytest.raises(InputError) as ei:
        parse_and_normalize(url, SCHEMES)
    assert ei.value.code == code, f"{url}: {ei.value.code} != {code}"


def test_domain_trailing_dot_and_case():
    p = parse_and_normalize("http://EXAMPLE.COM./a", SCHEMES)
    assert p.host == "example.com"
