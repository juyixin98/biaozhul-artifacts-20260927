"""诊断脱敏与请求标识测试。"""

from __future__ import annotations

import io
import json

from teachchain.diagnostics import Diagnostics, redact, sanitize


def test_redacts_sensitive_b64_fields():
    out = sanitize({
        "sig_b64": "A" * 100,
        "pub_b64": "B" * 60,
        "code_b64": "C" * 200,
        "gas": 123,
        "tx": "abcdef0123456789",
    })
    assert out["sig_b64"] == "<redacted:100 chars>"
    assert out["pub_b64"] == "<redacted:60 chars>"
    assert out["code_b64"] == "<redacted:200 chars>"
    assert out["gas"] == 123
    assert out["tx"] == "abcdef0123456789"  # 假名标识保留


def test_redact_non_string_passthrough():
    assert redact("sig_b64", 123) == 123
    assert redact("other", "plain") == "plain"


def test_diagnostics_emits_structured_line_with_request_id():
    buf = io.StringIO()
    diag = Diagnostics(logger_name="test_diag_unique", stream=buf)
    diag("warning", "bad_nonce",
         {"sender": "0xab", "got": 5, "expected": 4},
         request_id="req-xyz")
    line = buf.getvalue().strip()
    rec = json.loads(line)
    assert rec["level"] == "warning"
    assert rec["code"] == "bad_nonce"
    assert rec["request_id"] == "req-xyz"
    assert rec["sender"] == "0xab"


def test_diagnostics_default_request_id_is_dash_not_host_time():
    buf = io.StringIO()
    diag = Diagnostics(logger_name="test_diag_unique2", stream=buf)
    diag("info", "ok", {"height": 1})
    rec = json.loads(buf.getvalue().strip())
    # 未注入 request_id 时用占位符，绝不偷偷取时间/uuid
    assert rec["request_id"] == "-"
