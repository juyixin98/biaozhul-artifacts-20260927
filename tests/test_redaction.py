"""Redaction + diagnostics tests: raw values must never leak into records/logs."""

from __future__ import annotations

import json
import logging

import pytest

from sqlguard.core.redaction import redact_value, redact_bindings, value_type_name
from sqlguard.logging_setup import JsonFormatter


def test_scalar_redaction_does_not_contain_value():
    secret = "password123!"
    d = redact_value(secret)
    assert d == {"type": "str", "length": len(secret)}
    assert secret not in json.dumps(d)


def test_int_redaction_exposes_only_shape():
    d = redact_value(-12345)
    assert d == {"type": "int", "signed": True, "magnitude_digits": 5}


def test_array_redaction_lists_types_only():
    d = redact_value(["secret1", "secret2"])
    assert d == {"type": "array", "length": 2, "element_types": ["str"]}


def test_none_and_bool_are_shape_not_data():
    assert redact_value(None)["type"] == "null"
    assert redact_value(True) == {"type": "bool", "value": True}


def test_bindings_dict_and_list_shapes():
    d = redact_bindings({"0": "x", "name": "y"})
    assert d["0"] == {"type": "str", "length": 1}
    l = redact_bindings(["a", 1])
    assert l[0]["type"] == "str" and l[1]["type"] == "int"


def test_rejected_object_type_carries_no_object_content():
    d = redact_value({"ssn": "123-45-6789"})
    assert d == {"type": "object"}


@pytest.mark.parametrize("v,expected", [
    (1, "int"), (1.0, "float"), ("s", "str"), (b"x", "bytes"),
    (True, "bool"), (None, "null"), ([1], "array"), ({}, "object"),
])
def test_type_names(v, expected):
    assert value_type_name(v) == expected


def test_json_log_line_carries_request_id_and_redacted_params():
    record = logging.LogRecord(
        name="sqlguard", level=logging.INFO, pathname="", lineno=0,
        msg="review complete", args=(), exc_info=None)
    record.request_id = "req_abc"
    record.verdict = "reject"
    record.codes = {"reject": ["MISSING_BINDING"], "unanalyzable": [],
                    "advisory": []}
    record.stmt = "SELECT"
    record.params_redacted = redact_bindings({"0": "CUSTOMER_PII_VALUE"})
    line = JsonFormatter().format(record)
    parsed = json.loads(line)
    assert parsed["request_id"] == "req_abc"
    assert parsed["verdict"] == "reject"
    assert "CUSTOMER_PII_VALUE" not in line
    assert parsed["params_redacted"]["0"]["length"] == len("CUSTOMER_PII_VALUE")
