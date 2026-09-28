"""日志自身不泄漏原文；位置映射可被独立校验；规则配置校验。"""
from __future__ import annotations

import json
import logging

import pytest

from app.audit import AuditService
from app.core.redactor import redact_whole
from app.state.logging_utils import SAFE_LOGGER
from tests.fixtures import BANK_CARD, SAMPLE_LOG, TOKEN


def test_logs_contain_no_secret_text(registry):
    """在安全 logger 上挂一个外部 handler（模拟采集器），确认明文到不了它。"""
    captured: list[str] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    before = SAFE_LOGGER.dropped_leaky_records
    SAFE_LOGGER.register_request_secrets(
        [BANK_CARD, TOKEN, "P@ssw0rd-SYNTHETIC-9931"])
    h = Collector()
    SAFE_LOGGER._logger.addHandler(h)
    try:
        redact_whole(registry.get("standard"), SAMPLE_LOG)
        SAFE_LOGGER.info("debug dump %s", BANK_CARD)  # 必须被丢弃
        SAFE_LOGGER.info("normal request profile=standard")
    finally:
        SAFE_LOGGER._logger.removeHandler(h)

    text = "\n".join(captured)
    assert BANK_CARD not in text
    assert TOKEN not in text
    assert "P@ssw0rd-SYNTHETIC-9931" not in text
    # 泄漏记录被计数丢弃（过滤发生的权威证据）
    assert SAFE_LOGGER.dropped_leaky_records >= before + 1
    # 正常元数据日志保留
    assert "normal request profile=standard" in text


def test_independent_offset_verifier_on_api(client):
    token = client.audit_token
    body = client.post("/api/v1/redact", json={"text": SAMPLE_LOG}).json()
    rid = body["request_id"]
    detail = client.get(f"/api/v1/audit/requests/{rid}",
                        headers={"X-Audit-Token": token}).json()
    out = detail["redacted_output"]
    problems = []
    prev = 0
    for i, m in enumerate(detail["mappings"]):
        os_, oe_ = m["output_span"]
        if os_ < prev:
            problems.append(f"{i} 重叠")
        if out[os_:oe_] != m["replacement"]:
            problems.append(f"{i} 区间内容不符")
        prev = oe_
    assert problems == []


def test_audit_service_verify_helper(registry):
    result = redact_whole(registry.get("standard"), SAMPLE_LOG)
    problems = AuditService.verify_offset_consistency(
        result.output, result.mappings)
    assert problems == []


# --------------------------------------------------------------------- #
# 规则配置必须在加载期失败
# --------------------------------------------------------------------- #
def test_parser_rejects_bad_regex(tmp_path):
    from app.rules.parser import parse_registry, RuleConfigError
    doc = _base_doc()
    doc["profiles"][0]["rules"][0]["pattern"] = "([0-9]+"
    with pytest.raises(RuleConfigError):
        parse_registry(doc)


def test_parser_rejects_unanchored_hint(tmp_path):
    from app.rules.parser import parse_registry, RuleConfigError
    doc = _base_doc()
    doc["profiles"][0]["rules"][0]["prefix_hint"] = "\\d{1,16}"
    with pytest.raises(RuleConfigError):
        parse_registry(doc)


def test_parser_rejects_duplicate_profile(tmp_path):
    from app.rules.parser import parse_registry, RuleConfigError
    doc = _base_doc()
    doc["profiles"].append(json.loads(json.dumps(doc["profiles"][0])))
    with pytest.raises(RuleConfigError):
        parse_registry(doc)


def test_parser_rejects_missing_default(tmp_path):
    from app.rules.parser import parse_registry, RuleConfigError
    doc = _base_doc()
    doc["default_profile"] = "ghost"
    with pytest.raises(RuleConfigError):
        parse_registry(doc)


def _base_doc():
    import pathlib
    p = pathlib.Path(__file__).resolve().parent.parent / "config" / "rules.json"
    return json.loads(p.read_text("utf-8"))
