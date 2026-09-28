"""Redaction helpers.

Audit records and logs must explain *why* a verdict was reached without
leaking customer values. Only structural facts survive: parameter marker,
context, value type, length / range, and membership verdict. Identifier slot
values are not user-secret data (they come from a declared whitelist) and may
be shown verbatim.
"""

from __future__ import annotations

from typing import Any


def value_type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, bytes):
        return "bytes"
    if isinstance(v, (list, tuple)):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def redact_value(v: Any) -> dict[str, Any]:
    """Return a safe descriptor of *v* — never the value itself."""
    tn = value_type_name(v)
    out: dict[str, Any] = {"type": tn}
    if tn in ("int", "float"):
        out["signed"] = v < 0
        out["magnitude_digits"] = len(str(abs(v)).split(".")[0])
    elif tn == "str":
        out["length"] = len(v)
    elif tn == "bytes":
        out["length"] = len(v)
    elif tn == "bool":
        out["value"] = v  # bools carry no user entropy
    elif tn == "array":
        out["length"] = len(v)
        out["element_types"] = sorted({value_type_name(x) for x in v})
    elif tn == "null":
        out["value"] = None
    return out


def redact_bindings(params: Any) -> dict[str, Any]:
    """Redact a whole params structure (list or dict)."""
    if isinstance(params, dict):
        return {str(k): redact_value(v) for k, v in params.items()}
    if isinstance(params, (list, tuple)):
        return [redact_value(v) for v in params]
    return {}
