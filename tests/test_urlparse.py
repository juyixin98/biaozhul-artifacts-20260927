"""受控 URL 解析与主机规范化 —— 断言具体结果与失败类别/原因码。"""
from __future__ import annotations

import pytest

from app.contracts import FailureKind, Reason
from app.urlparse import parse_target


def _expect_input_error(url, reason):
    with pytest.raises(Exception) as ei:
        parse_target(url)
    err = ei.value
    assert err.kind == FailureKind.INPUT_ERROR, url
    assert err.reason == reason, (url, err.reason)


def test_plain_http_normalized_with_explicit_port():
    p = parse_target("http://example.com/path?q=1#frag")
    assert p.url == "http://example.com:80/path?q=1"  # fragment 被丢弃
    assert p.host == "example.com"
    assert p.host_kind == "dns"
    assert p.port == 80


def test_idna_unicode_host_normalized_to_ascii():
    p = parse_target("https://例え.jp/")
    assert p.host == "xn--r8jz45g.jp"
    assert p.host_kind == "dns"
    assert p.port == 443


def test_trailing_dot_dns_stripped():
    p = parse_target("http://example.com./x")
    assert p.host == "example.com"


def test_ipv4_literal_classified_as_ipv4():
    p = parse_target("http://127.0.0.1:8080/")
    assert p.host_kind == "ipv4"
    assert p.host == "127.0.0.1"
    assert p.port == 8080
    assert p.normalized_for_lookup == "127.0.0.1"


def test_ipv6_literal_compressed():
    p = parse_target("http://[0:0:0:0:0:0:0:1]/")
    assert p.host_kind == "ipv6"
    assert p.normalized_for_lookup == "::1"


def test_mapped_ipv6_preserved_for_classifier():
    p = parse_target("http://[::ffff:169.254.169.254]/")
    assert p.host_kind == "ipv6"
    assert p.normalized_for_lookup == "::ffff:a9fe:a9fe"


@pytest.mark.parametrize("url,reason", [
    ("", Reason.URL_MALFORMED),
    ("   ", Reason.URL_MALFORMED),
    ("file:///etc/passwd", Reason.SCHEME_UNSUPPORTED),
    ("ftp://1.2.3.4/", Reason.SCHEME_UNSUPPORTED),
    ("gopher://x/", Reason.SCHEME_UNSUPPORTED),
    ("http:///nohost", Reason.HOST_MISSING),
    ("http://user:pass@example.com/", Reason.USERINFO_FORBIDDEN),
    ("http://169.254.169.254@example.com/", Reason.USERINFO_FORBIDDEN),
    ("http://user@127.0.0.1/", Reason.USERINFO_FORBIDDEN),
    ("http://2130706433/", Reason.HOST_INTEGER_IP),
    ("http://0x7f000001/", Reason.HOST_INTEGER_IP),
    ("http://0177.0.0.1/", Reason.HOST_AMBIGUOUS_NUMERIC),
    ("http://127.1/", Reason.HOST_AMBIGUOUS_NUMERIC),
    ("http://127.0.0.1\\@evil.com/", Reason.URL_BAD_CHARACTER),
    ("http://exa mple.com/", Reason.URL_BAD_CHARACTER),
    ("http://example.com:99999/", Reason.PORT_INVALID),
    ("http://example.com:abc/", Reason.PORT_INVALID),
    ("http://[not-ipv6]/", Reason.URL_MALFORMED),
    ("http://bad_label-.com/", Reason.HOST_INVALID_LABEL),
])
def test_input_errors(url, reason):
    _expect_input_error(url, reason)


def test_default_ports_per_scheme():
    assert parse_target("http://h/").port == 80
    assert parse_target("https://h/").port == 443


def test_path_defaults_to_slash():
    assert parse_target("http://h").path == "/"
    assert parse_target("http://h").url == "http://h:80/"
