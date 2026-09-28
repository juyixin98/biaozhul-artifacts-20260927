"""规则档解析与校验。

加载时必须发现的错误（启动期失败，而非请求期）：
- 正则无法编译
- max_length 非正、提示未锚定
- replacement 含换行或为空
- 键集合为空、id 重复
- profile 名称重复、默认档缺失
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import FieldRule, PatternRule, Profile, RuleConfigError

_VALID_TAIL_MODES = {"mark", "error"}


def _build_pattern_rule(raw: dict) -> PatternRule:
    required = ("id", "pattern", "max_length", "replacement", "prefix_hint")
    for key in required:
        if key not in raw:
            raise RuleConfigError(f"pattern rule 缺少字段: {key!r}")
    rule_id = str(raw["id"])
    if not rule_id:
        raise RuleConfigError("pattern rule id 不能为空")
    try:
        compiled = re.compile(raw["pattern"])
    except re.error as exc:
        raise RuleConfigError(f"规则 {rule_id} 正则无法编译: {exc}") from exc
    try:
        hint = re.compile(raw["prefix_hint"])
    except re.error as exc:
        raise RuleConfigError(f"规则 {rule_id} prefix_hint 无法编译: {exc}") from exc
    # prefix_hint 用于在 hold 尾部搜索，必须锚定行尾，否则它会匹配任意前缀
    if not raw["prefix_hint"].endswith("$"):
        raise RuleConfigError(f"规则 {rule_id} prefix_hint 必须以 $ 锚定结尾")
    tail_suspicion = None
    tail_suspicion_source = None
    if raw.get("tail_suspicion"):
        src = str(raw["tail_suspicion"])
        if not src.endswith("$"):
            raise RuleConfigError(
                f"规则 {rule_id} tail_suspicion 必须以 $ 锚定结尾")
        try:
            tail_suspicion = re.compile(src)
        except re.error as exc:
            raise RuleConfigError(
                f"规则 {rule_id} tail_suspicion 无法编译: {exc}") from exc
        tail_suspicion_source = src
    max_length = raw["max_length"]
    if not isinstance(max_length, int) or isinstance(max_length, bool) or max_length < 1:
        raise RuleConfigError(f"规则 {rule_id} max_length 必须为正整数")
    replacement = str(raw["replacement"])
    if not replacement or "\n" in replacement or "\r" in replacement:
        raise RuleConfigError(f"规则 {rule_id} replacement 不能为空或包含换行")
    priority = raw.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
        raise RuleConfigError(f"规则 {rule_id} priority 必须为非负整数")
    return PatternRule(
        id=rule_id,
        description=str(raw.get("description", "")),
        pattern=compiled,
        pattern_source=raw["pattern"],
        priority=priority,
        max_length=max_length,
        replacement=replacement,
        prefix_hint=hint,
        prefix_hint_source=raw["prefix_hint"],
        tail_suspicion=tail_suspicion,
        tail_suspicion_source=tail_suspicion_source,
    )


def _build_field_rule(raw: dict) -> FieldRule:
    rule_id = str(raw.get("id", "field-rule"))
    keys = raw.get("keys")
    if not isinstance(keys, list) or not keys or not all(
        isinstance(k, str) and k for k in keys
    ):
        raise RuleConfigError(f"字段规则 {rule_id} keys 必须为非空字符串列表")
    max_len = raw.get("max_value_length", 256)
    if not isinstance(max_len, int) or isinstance(max_len, bool) or max_len < 1:
        raise RuleConfigError(f"字段规则 {rule_id} max_value_length 必须为正整数")
    replacement = str(raw.get("replacement", "<FIELD>"))
    if not replacement or "\n" in replacement or "\r" in replacement:
        raise RuleConfigError(f"字段规则 {rule_id} replacement 非法")
    priority = raw.get("priority", 50)
    if not isinstance(priority, int) or isinstance(priority, bool) or priority < 0:
        raise RuleConfigError(f"字段规则 {rule_id} priority 必须为非负整数")
    return FieldRule(
        id=rule_id,
        description=str(raw.get("description", "")),
        keys=frozenset(keys),
        priority=priority,
        max_value_length=max_len,
        replacement=replacement,
    )


def _build_profile(raw: dict) -> Profile:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise RuleConfigError("profile name 必须为非空字符串")
    version = str(raw.get("version", "0"))
    tail_mode = str(raw.get("on_ambiguous_tail", "mark"))
    if tail_mode not in _VALID_TAIL_MODES:
        raise RuleConfigError(
            f"profile {name} on_ambiguous_tail 必须为 {sorted(_VALID_TAIL_MODES)} 之一"
        )
    rules_raw = raw.get("rules", [])
    if not isinstance(rules_raw, list) or not rules_raw:
        raise RuleConfigError(f"profile {name} 至少需要一条模式规则")
    pattern_rules = tuple(_build_pattern_rule(r) for r in rules_raw)
    ids = [r.id for r in pattern_rules]
    if len(set(ids)) != len(ids):
        raise RuleConfigError(f"profile {name} 存在重复规则 id: {ids}")
    field_rule = _build_field_rule(raw["field_rule"]) if raw.get("field_rule") else None
    return Profile(
        name=name,
        version=version,
        description=str(raw.get("description", "")),
        on_ambiguous_tail=tail_mode,  # type: ignore[arg-type]
        pattern_rules=pattern_rules,
        field_rule=field_rule,
    )


@dataclass(frozen=True)
class Registry:
    """规则档注册表：一次加载、只读使用。"""

    default_profile: str
    profiles: dict[str, Profile]

    def get(self, name: str | None = None) -> Profile:
        key = name or self.default_profile
        try:
            return self.profiles[key]
        except KeyError:
            raise UnknownProfile(key) from None


class UnknownProfile(KeyError):
    """请求引用了不存在的规则档。"""


def parse_registry(doc: dict) -> Registry:
    if not isinstance(doc, dict):
        raise RuleConfigError("规则文档根必须为 JSON 对象")
    profiles_raw = doc.get("profiles")
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise RuleConfigError("profiles 必须为非空列表")
    profiles: dict[str, Profile] = {}
    for raw in profiles_raw:
        prof = _build_profile(raw)
        if prof.name in profiles:
            raise RuleConfigError(f"profile 名称重复: {prof.name}")
        profiles[prof.name] = prof
    default = str(doc.get("default_profile", ""))
    if default not in profiles:
        raise RuleConfigError(f"default_profile={default!r} 不在 profiles 中")
    return Registry(default_profile=default, profiles=profiles)


def load_registry(path: str | Path) -> Registry:
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuleConfigError(f"规则文件不存在: {p}") from exc
    except json.JSONDecodeError as exc:
        raise RuleConfigError(f"规则文件 JSON 非法: {exc}") from exc
    return parse_registry(doc)
