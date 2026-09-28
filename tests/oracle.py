"""独立参考实现（oracle）。

不导入 app.core 的任何模块；用整段、与内核不同的实现路径（正则切片 +
独立手写的 JSON 键值界定）给出"期望输出/期望映射"。测试以 oracle 的
具体结果为断言基准，从而避免"答案由被测核心自身生成"。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class OracleMatch:
    rule_id: str
    source: str  # pattern | field
    key: str | None
    start: int
    end: int
    replacement: str
    priority: int
    text: str


# --------------------------------------------------------------------- #
# 独立的整段 JSON 敏感键值界定（与 StreamingJsonLexer 无共享代码）
# --------------------------------------------------------------------- #
def _scan_string(s: str, i: int) -> int | None:
    """s[i] == '"'，返回闭合引号后的位置；非法/未闭合返回 None。"""
    assert s[i] == '"'
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n or s[i + 1] not in '"\\/bfnrtu':
                return None
            if s[i + 1] == "u":
                if i + 5 >= n + 1 or not re.fullmatch(
                        r"[0-9a-fA-F]{4}", s[i + 2:i + 6]):
                    return None
                i += 6
                continue
            i += 2
            continue
        if c == '"':
            return i + 1
        i += 1
    return None


def find_field_values(text: str, keys_lower: set[str],
                      field_rule_id: str, replacement: str,
                      priority: int) -> list[OracleMatch]:
    out: list[OracleMatch] = []
    # 键：完整 JSON 字符串字面量
    key_re = re.compile(r'"((?:[^"\\]|\\.)*)"')
    for km in key_re.finditer(text):
        try:
            key = json.loads(km.group(0))
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(key, str) or key.lower() not in keys_lower:
            continue
        j = km.end()
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j >= len(text) or text[j] != ":":
            continue
        j += 1
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        if j >= len(text):
            continue
        start = j
        end: int | None
        if text[j] == '"':
            end = _scan_string(text, j)
            if end is None:
                continue
        elif text[j] in "-0123456789tfn":
            k = j + 1
            while k < len(text) and text[k] not in ",]} \t\r\n":
                k += 1
            end = k
        else:
            continue
        out.append(OracleMatch(
            rule_id=field_rule_id, source="field", key=key,
            start=start, end=end, replacement=replacement,
            priority=priority, text=text[start:end]))
    return out


# --------------------------------------------------------------------- #
def _resolve(matches: list[OracleMatch]) -> list[OracleMatch]:
    ordered = sorted(matches,
                     key=lambda m: (m.priority, m.start, -m.end,
                                    0 if m.source == "field" else 1,
                                    m.rule_id))
    accepted: list[OracleMatch] = []
    for m in ordered:
        if any(m.start < a.end and a.start < m.end for a in accepted):
            continue
        accepted.append(m)
    return sorted(accepted, key=lambda m: m.start)


def oracle_redact(text: str, profile_doc: dict) -> dict:
    """profile_doc 为 rules.json 中单个 profile 的原始 dict。"""
    matches: list[OracleMatch] = []
    for r in profile_doc["rules"]:
        rx = re.compile(r["pattern"])
        for m in rx.finditer(text):
            matches.append(OracleMatch(
                rule_id=r["id"], source="pattern", key=None,
                start=m.start(), end=m.end(),
                replacement=r["replacement"], priority=r.get("priority", 100),
                text=m.group(0)))
    fr = profile_doc.get("field_rule")
    if fr:
        matches.extend(find_field_values(
            text, {k.lower() for k in fr["keys"]}, fr["id"],
            fr["replacement"], fr.get("priority", 50)))

    accepted = _resolve(matches)

    # 从右向左替换，偏移不失效；同时记录输出区间
    out = text
    spans: list[dict] = []
    for m in reversed(accepted):
        out = out[:m.start] + m.replacement + out[m.end:]
    # 重新顺序计算输出区间（长度变化后的正确位置）
    delta = 0
    for m in accepted:
        os_ = m.start + delta
        oe_ = os_ + len(m.replacement)
        spans.append({
            "rule_id": m.rule_id, "source": m.source, "key": m.key,
            "original_span": [m.start, m.end],
            "output_span": [os_, oe_],
            "replacement": m.replacement,
            "text": m.text,
        })
        delta += len(m.replacement) - (m.end - m.start)
    return {
        "output": out,
        "spans": spans,
        "matched_texts": sorted({m.text for m in accepted}),
        "rule_ids": sorted({m.rule_id for m in accepted}),
    }
