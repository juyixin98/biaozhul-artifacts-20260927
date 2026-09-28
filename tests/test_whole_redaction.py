"""整段脱敏与独立 oracle 的一致性 + 具体断言。"""
from __future__ import annotations

from app.core.redactor import redact_whole
from tests.fixtures import (
    ADJACENT_TEXT,
    BANK_CARD,
    EMAIL,
    EMP_ID,
    MOBILE,
    PASSWORD,
    SAMPLE_LOG,
    TOKEN,
)
from tests.oracle import oracle_redact


def test_whole_matches_independent_oracle_standard(registry, profile_docs):
    profile = registry.get("standard")
    doc = profile_docs["standard"]
    result = redact_whole(profile, SAMPLE_LOG)

    assert result.status == "ok", result.error_message
    expected = oracle_redact(SAMPLE_LOG, doc)
    # 与独立参考实现逐字符一致
    assert result.output == expected["output"]
    # 映射条数一致、区间一致
    assert len(result.mappings) == len(expected["spans"])
    for got, exp in zip(result.mappings, expected["spans"]):
        assert got.original_start == exp["original_span"][0]
        assert got.original_end == exp["original_span"][1]
        assert got.output_start == exp["output_span"][0]
        assert got.output_end == exp["output_span"][1]
        assert got.replacement == exp["replacement"]
        assert got.rule_id == exp["rule_id"]


def test_concrete_replacements_present(registry):
    result = redact_whole(registry.get("standard"), SAMPLE_LOG)
    out = result.output
    # 每类合成秘密都应被对应的占位符替换
    assert "<BANK_CARD>" in out
    assert "<MOBILE>" in out
    assert "<TOKEN>" in out
    assert "<EMP_ID>" in out
    assert "<EMAIL>" in out
    # 字段规则：密码值（含特殊字符）替换为 <FIELD>
    assert "<FIELD>" in out


def test_no_raw_secret_residual_anywhere(registry):
    result = redact_whole(registry.get("standard"), SAMPLE_LOG)
    for secret in (BANK_CARD, MOBILE, TOKEN, EMP_ID, EMAIL, PASSWORD):
        assert secret not in result.output, f"{secret} 残留在输出中"
    # 内核自检也应为零发现
    assert result.residual_findings == []


def test_length_delta_consistency(registry):
    result = redact_whole(registry.get("standard"), SAMPLE_LOG)
    delta = sum(m.output_end - m.output_start
                - (m.original_end - m.original_start)
                for m in m_span_iter(result))
    assert result.output_length - result.original_length == delta
    assert len(result.output) == result.output_length


def m_span_iter(result):
    return result.mappings


def test_mapping_sha256_and_replace_position(registry):
    import hashlib
    result = redact_whole(registry.get("standard"), SAMPLE_LOG)
    for m in result.mappings:
        original = SAMPLE_LOG[m.original_start:m.original_end]
        assert m.original_sha256 == hashlib.sha256(
            original.encode()).hexdigest()
        assert result.output[m.output_start:m.output_end] == m.replacement
    # 映射按原文起点有序
    starts = [m.original_start for m in result.mappings]
    assert starts == sorted(starts)


def test_adjacent_rules_both_fire(registry):
    # 卡号与工号紧邻无分隔（同一非敏感字段值内）：两条规则都应命中
    result = redact_whole(registry.get("standard"), ADJACENT_TEXT)
    assert result.status == "ok"
    assert BANK_CARD not in result.output
    assert EMP_ID not in result.output
    assert result.output == '{"a":"<BANK_CARD>","b":"<EMP_ID>"}'
    ids = {m.rule_id for m in result.mappings}
    assert {"bank-card-16", "emp-id"} <= ids
    # 相邻（端点相接）不算重叠：无拒绝记录
    assert result.rejected == []
