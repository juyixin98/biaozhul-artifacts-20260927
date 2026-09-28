"""规则模型：模式规则、字段规则、规则档（profile）。

优先级约定：priority 数值越小优先级越高；模式规则与字段规则在同一
候选区间上竞争时，数值小的胜出。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

OnAmbiguousTail = Literal["mark", "error"]


class RuleConfigError(ValueError):
    """规则配置非法（无法编译、长度矛盾、字段为空等）。"""


@dataclass(frozen=True)
class PatternRule:
    id: str
    description: str
    pattern: re.Pattern[str]
    pattern_source: str
    priority: int
    max_length: int
    replacement: str
    prefix_hint: re.Pattern[str]
    prefix_hint_source: str
    tail_suspicion: re.Pattern[str] | None = None
    tail_suspicion_source: str | None = None
    type: str = "pattern"

    def has_possible_prefix(self, suffix: str) -> bool:
        """suffix 的某个尾部是否可能是该模式某个完整匹配的前缀。

        用于流式安全边界计算：只要某条规则的提示还能在 hold 尾部
        匹配，就不能在该位置之前放行（避免部分原值泄漏）。
        """
        return self.prefix_hint.search(suffix) is not None


@dataclass(frozen=True)
class FieldRule:
    id: str
    description: str
    keys: frozenset[str]
    priority: int
    max_value_length: int
    replacement: str
    type: str = "field"

    def is_sensitive_key(self, key: str) -> bool:
        # 键名匹配大小写不敏感：JSON 键名常见大小写变体
        return key.lower() in self.keys_lower

    @property
    def keys_lower(self) -> frozenset[str]:
        return frozenset(k.lower() for k in self.keys)


@dataclass(frozen=True)
class Profile:
    name: str
    version: str
    description: str
    on_ambiguous_tail: OnAmbiguousTail
    pattern_rules: tuple[PatternRule, ...]
    field_rule: FieldRule | None = field(default=None)

    def find_rule(self, rule_id: str) -> PatternRule | FieldRule | None:
        if self.field_rule and self.field_rule.id == rule_id:
            return self.field_rule
        for r in self.pattern_rules:
            if r.id == rule_id:
                return r
        return None
