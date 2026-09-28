"""尾部不完整片段（未完整识别不得放行）与规则档切换。"""
from __future__ import annotations

import pytest

from app.core.redactor import StreamingRedactor, redact_whole
from tests.fixtures import BANK_CARD, MOBILE, TOKEN


def test_truncated_card_marked_in_standard(registry):
    # 结尾是 13 位数字：token 边界起始，疑似被截断的卡号
    text = "log entry 6225123456789 end"
    r = redact_whole(registry.get("standard"), text)
    codes = [u.code for u in r.uncertainties]
    assert "AMBIGUOUS_TAIL" in codes
    # 可疑片段在 mark 模式下不伪装成成功：状态仍给出明确不确定结论
    # 且前缀（含该数字）不会被当作完整卡号静默替换
    assert not any(m.rule_id == "bank-card-16" for m in r.mappings)


def test_truncated_card_errors_in_strict(registry):
    text = "log entry 6225123456789 end"
    r = redact_whole(registry.get("strict"), text)
    assert r.status == "error"
    assert r.error_code == "AMBIGUOUS_TAIL"
    assert any(u.code == "AMBIGUOUS_TAIL" for u in r.uncertainties)


def test_complete_card_no_ambiguity(registry):
    # 完整 16 位卡号正常替换，不报尾部歧义
    text = f"log entry {BANK_CARD} end"
    r = redact_whole(registry.get("standard"), text)
    assert r.status == "ok"
    assert not any(u.code == "AMBIGUOUS_TAIL" for u in r.uncertainties)
    assert r.output == f"log entry <BANK_CARD> end"


def test_trailing_alnum_guarded_against_false_alarm(registry):
    # 短标识符与超长数字（落在所有 suspicion 长度区间之外）不应报警
    r1 = redact_whole(registry.get("standard"), "ref id abc123 done")
    assert not any(u.code == "AMBIGUOUS_TAIL" for u in r1.uncertainties)
    # 超过最大长度（32）的字母数字串不可能是某规则的"前缀"，不报警
    r2 = redact_whole(registry.get("standard"),
                      "ref " + ("a" * 40) + " done")
    assert not any(u.code == "AMBIGUOUS_TAIL" for u in r2.uncertainties)


def test_truncated_token_held_in_streaming(registry):
    # 结尾只到令牌前 20 位：逐块到达时，这些字符在 finalize 前不得发出
    prefix = TOKEN[:20]
    assert len(prefix) == 20
    red = StreamingRedactor(registry.get("standard"))
    emitted = ""
    for ch in "data " + prefix:
        emitted += red.feed(ch).text
    # finalize 前令牌前缀未流出（前缀出现在 'data ' 之后）
    assert prefix not in emitted
    result = red.finalize()
    assert any(u.code == "AMBIGUOUS_TAIL" for u in result.uncertainties)


def test_profile_switch_changes_field_keys(registry):
    # strict 档额外把 card_no 视为敏感字段；standard 不识别
    text = '{"card_no":"6225123456789010"}'
    std = redact_whole(registry.get("standard"), text)
    strict = redact_whole(registry.get("strict"), text)
    # standard：card_no 非敏感字段，卡号由模式规则替换，保留引号
    assert std.output == '{"card_no":"<BANK_CARD>"}'
    # strict：card_no 是敏感字段，整个值（含引号）替换为 <FIELD>
    assert strict.output == '{"card_no":<FIELD>}'
    strict_sources = {m.source for m in strict.mappings}
    assert "field" in strict_sources
    std_field = [m for m in std.mappings if m.source == "field"]
    assert all(m.key != "card_no" for m in std_field)


def test_profile_version_recorded(registry):
    r = redact_whole(registry.get("strict"), "hi")
    assert r.profile_name == "strict"
    assert r.profile_version == registry.get("strict").version
