"""JSON 字段识别：转义、嵌套、数字值、非法结构降级、键大小写。"""
from __future__ import annotations

from app.core.redactor import redact_whole
from tests.fixtures import BANK_CARD, PASSWORD, SECRET_NUMBER


def test_string_field_with_escaped_quote(registry):
    text = r'{"user":"a","password":"p\"q","x":1}'
    r = redact_whole(registry.get("standard"), text)
    assert r.status == "ok"
    assert r.output == r'{"user":"a","password":<FIELD>,"x":1}'
    assert 'p"q' not in r.output


def test_string_field_with_backslash_and_unicode(registry):
    text = r'{"secret":"a\\b中c"}'
    r = redact_whole(registry.get("standard"), text)
    assert r.status == "ok"
    assert r.output == '{"secret":<FIELD>}'


def test_numeric_field_value_redacted(registry):
    # 数字型敏感值（不带引号）整体替换
    text = '{"id":1,"token":1357902468,"z":0}'
    r = redact_whole(registry.get("standard"), text)
    assert r.status == "ok"
    assert SECRET_NUMBER not in r.output
    assert r.output == '{"id":1,"token":<FIELD>,"z":0}'


def test_nested_object_sensitive_key(registry):
    text = '{"outer":{"api_key":"abc123def456ghi789jkl012mno345pq"}}'
    r = redact_whole(registry.get("standard"), text)
    assert r.status == "ok"
    assert "abc123def456ghi789jkl012mno345pq" not in r.output
    assert r.output == '{"outer":{"api_key":<FIELD>}}'


def test_case_insensitive_key(registry):
    text = '{"PASSWORD":"hunter2syn"}'
    r = redact_whole(registry.get("standard"), text)
    assert r.status == "ok"
    assert r.output == '{"PASSWORD":<FIELD>}'


def test_non_sensitive_key_kept(registry):
    text = '{"username":"alice.synth","note":"6225123456789010"}'
    r = redact_whole(registry.get("standard"), text)
    # note 不是敏感键，但其值是 16 位卡号 → 模式规则仍应命中
    assert r.output == '{"username":"alice.synth","note":"<BANK_CARD>"}'


def test_invalid_escape_disables_field_but_patterns_continue(registry):
    # 非法 JSON 转义 \x：字段识别停用并报不确定；模式规则仍工作
    text = r'{"password":"abc\xyz"} card 6225123456789010'
    r = redact_whole(registry.get("standard"), text)
    codes = {u.code for u in r.uncertainties}
    assert "INVALID_ESCAPE" in codes
    # 模式规则不受影响
    assert BANK_CARD not in r.output
    assert "<BANK_CARD>" in r.output


def test_unclosed_sensitive_string_replaced_and_flagged(registry):
    # 流在未闭合的敏感字符串中间结束：已见内容必须替换，且单列不确定
    text = '{"password":"abcdef'
    r = redact_whole(registry.get("standard"), text)
    assert "abcdef" not in r.output
    codes = [u.code for u in r.uncertainties]
    assert "UNCLOSED_STRING" in codes


def test_field_value_length_limits(registry):
    # 超长值：界定并替换，同时给出 OVERLONG_FIELD_VALUE 不确定
    long_val = "x" * 300
    text = '{"password":"' + long_val + '"}'
    r = redact_whole(registry.get("standard"), text)
    assert long_val not in r.output
    assert "OVERLONG_FIELD_VALUE" in [u.code for u in r.uncertainties]


def test_plain_prose_passes_through(registry):
    text = "nothing json here, just a log line 12345"
    r = redact_whole(registry.get("standard"), text)
    assert r.status == "ok"
    assert r.output == text
    assert r.mappings == []
