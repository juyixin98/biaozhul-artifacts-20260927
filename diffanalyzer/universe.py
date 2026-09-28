"""受限请求空间：前缀区域划分 + 有限维笛卡尔积穷举。

资源维度的“完备分割”（边界陷阱防护）：
- 取分析范围前缀与两版策略中所有“相关规则前缀”的并集作为边界 B。
- 任意键 k 归属唯一区域：B 中作为 k 前缀的最长边界 P。
- 区域见证：P=="" 时取空键 ""；否则取 P+TAIL（TAIL 是字母表中非 '/' 的字符）。
  该键不可能落入更长的边界区域（边界前缀都以 '/' 结尾，而它不以 '/' 结尾）。
因此每个区域的任意键对“是否以各规则前缀为前缀”的布尔向量完全一致，
单个见证即可代表整个区域——这同时闭合了 "logs" 误匹配 "logs-secret" 类边界。

其它维度取有限域：
- 主体：配置中的合成主体 + 匿名(None) + 未被任何规则列举的 "@other"
- 操作：两版规则操作 ∪ 分析范围操作，外加未被列举的 "@other"
- 条件属性：从规则条件的右值构造（阈值两侧、集合内外、glob 匹配/不匹配、
  CIDR 网内/网外/畸形值、属性缺失），从而 UNKNOWN/TRUE/FALSE 三分支都能被取到。
"""

from __future__ import annotations

import fnmatch
import ipaddress
from dataclasses import dataclass
from itertools import product
from typing import Any, Iterator, Optional

from .models import (
    ConditionOp,
    Policy,
    Request,
    Rule,
    SpaceTooLargeError,
    UniverseDefinitionError,
)

OTHER_PRINCIPAL = "@other"
OTHER_ACTION = "@other"
MISSING = object()          # 该请求不携带某属性
BAD_IP = "not-a-valid-ip"   # 畸形值：CIDR 条件必须据此给 UNKNOWN
TAIL_FALLBACK = "0"


@dataclass(frozen=True)
class Region:
    anchor: str       # 区域边界前缀
    witness: str      # 区域内的一个真实键
    in_scope: bool    # 见证是否落在分析范围内


@dataclass(frozen=True)
class Universe:
    regions: tuple[Region, ...]
    principals: tuple[Optional[str], ...]
    actions: tuple[str, ...]
    # 属性名 -> 该属性的有限取值域（含 MISSING）
    attribute_domains: dict[str, tuple[Any, ...]]
    size: int
    alphabet: tuple[str, ...]

    def iter_requests(self) -> Iterator[tuple[Region, Request]]:
        attrs = sorted(self.attribute_domains)
        val_lists = [self.attribute_domains[a] for a in attrs]
        for region, principal, action, combo in product(
            self.regions, self.principals, self.actions, product(*val_lists)
        ):
            present = {
                name: val
                for name, val in zip(attrs, combo)
                if val is not MISSING
            }
            yield region, Request.make(principal, action, region.witness, present)


# ---------------------------------------------------------------------------
# 字母表校验
# ---------------------------------------------------------------------------
def validate_alphabet(alphabet: list[str]) -> str:
    if not isinstance(alphabet, list) or not alphabet:
        raise UniverseDefinitionError("resource_alphabet 必须是非空字符列表")
    if any(not isinstance(c, str) or len(c) != 1 for c in alphabet):
        raise UniverseDefinitionError("resource_alphabet 每项必须是单字符")
    if len(set(alphabet)) != len(alphabet):
        raise UniverseDefinitionError("resource_alphabet 含重复字符")
    if "/" not in alphabet:
        raise UniverseDefinitionError("resource_alphabet 必须包含目录分隔符 '/'")
    tail = next((c for c in alphabet if c != "/"), None)
    if tail is None:
        raise UniverseDefinitionError("resource_alphabet 至少包含一个非 '/' 字符")
    return tail


def _prefixes_overlap(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def build_regions(
    scope_prefixes: tuple[str, ...],
    relevant_rule_prefixes: set[str],
    tail: str,
) -> tuple[Region, ...]:
    scope_set = set(scope_prefixes)
    boundary = scope_set | {
        p for p in relevant_rule_prefixes
        if any(_prefixes_overlap(p, s) for s in scope_set)
    }

    regions: list[Region] = []
    for anchor in sorted(boundary):
        witness = "" if anchor == "" else anchor + tail
        in_scope = any(witness.startswith(s) for s in scope_set)
        if not in_scope:
            # 祖先前缀（如 scope=logs/ 时的桶根 ""）：其区域不在分析范围内，
            # 范围内部分由 scope 自身及更深的边界代表。
            continue
        regions.append(Region(anchor=anchor, witness=witness, in_scope=True))

    # 去重（不同 anchor 的见证不可能相同，这里仅作防御）
    seen: set[str] = set()
    unique: list[Region] = []
    for r in regions:
        if r.witness not in seen:
            seen.add(r.witness)
            unique.append(r)
    return tuple(unique)


# ---------------------------------------------------------------------------
# 主体 / 操作有限域
# ---------------------------------------------------------------------------
def build_principal_domain(
    configured: list[str], include_anonymous: bool, policies: list[Policy]
) -> tuple[Optional[str], ...]:
    named = set(configured)
    for p in policies:
        for r in p.rules:
            named.update(x for x in r.principals if x != "*")
    ordered: list[Optional[str]] = sorted(named)
    if include_anonymous:
        ordered.append(None)
    # 一个所有规则都未列举的合成主体：用于暴露“只给某些人开了口子”
    if OTHER_PRINCIPAL not in named:
        ordered.append(OTHER_PRINCIPAL)
    else:  # pragma: no cover - 配置撞名极少见，保守处理
        ordered.append(OTHER_PRINCIPAL + "__")
    return tuple(ordered)


def build_action_domain(
    scope_actions: frozenset[str], policies: list[Policy]
) -> tuple[str, ...]:
    acts = set(scope_actions)
    for p in policies:
        for r in p.rules:
            acts.update(r.actions)
    ordered = sorted(acts)
    ordered.append(OTHER_ACTION)
    return tuple(ordered)


# ---------------------------------------------------------------------------
# 属性有限域（保证 TRUE / FALSE / UNKNOWN 三分支可被取到）
# ---------------------------------------------------------------------------
def _other_string(existing: set[str]) -> str:
    cand = "~other~"
    i = 1
    while cand in existing:
        cand = f"~other{i}~"
        i += 1
    return cand


def build_attribute_domains(policies: list[Policy]) -> dict[str, tuple[Any, ...]]:
    # attr -> 取值集合
    dom: dict[str, set[Any]] = {}
    kind: dict[str, set[str]] = {}  # attr -> {"str","num","bool"}

    def note_kind(name: str, value: Any) -> None:
        if isinstance(value, bool):
            kind.setdefault(name, set()).add("bool")
        elif isinstance(value, (int, float)):
            kind.setdefault(name, set()).add("num")
        elif isinstance(value, str):
            kind.setdefault(name, set()).add("str")

    def add(name: str, value: Any) -> None:
        dom.setdefault(name, set()).add(value)
        note_kind(name, value)

    for p in policies:
        rule: Rule
        for rule in p.rules:
            for c in rule.conditions:
                dom.setdefault(c.attribute, set())
                kind.setdefault(c.attribute, set())
                op = c.op
                if op in (ConditionOp.EQ, ConditionOp.NOT_EQ):
                    add(c.attribute, c.value)
                elif op in (ConditionOp.IN, ConditionOp.NOT_IN):
                    for item in c.value:
                        add(c.attribute, item)
                elif op in (
                    ConditionOp.GREATER_THAN,
                    ConditionOp.LESS_THAN,
                    ConditionOp.GREATER_EQUAL,
                    ConditionOp.LESS_EQUAL,
                ):
                    t = c.value
                    add(c.attribute, t - 1)
                    add(c.attribute, t)
                    add(c.attribute, t + 1)
                elif op in (ConditionOp.CIDR_MATCH, ConditionOp.NOT_CIDR_MATCH):
                    net = ipaddress.ip_network(c.value, strict=False)
                    add(c.attribute, str(net.network_address))
                    add(c.attribute, str(net[-1]))
                    # 网外地址
                    outside = _address_outside(net)
                    if outside is not None:
                        add(c.attribute, outside)
                    add(c.attribute, BAD_IP)  # 畸形值 -> UNKNOWN
                elif op in (ConditionOp.GLOB_MATCH, ConditionOp.NOT_GLOB_MATCH):
                    matching = _glob_match_example(c.value)
                    if matching is not None:
                        add(c.attribute, matching)
                    nonmatching = "ZZ_no_match_ZZ"
                    if not fnmatch.fnmatchcase(nonmatching, c.value):
                        add(c.attribute, nonmatching)
                # EXISTS / NOT_EXISTS 只需“缺失/存在”：缺失由 MISSING 覆盖，
                # 存在值借用同属性其它条件；若属性上只有 Exists，放一个哨兵。
                if op in (ConditionOp.EXISTS, ConditionOp.NOT_EXISTS):
                    add(c.attribute, "present")

    # 每个属性：补充一个异值（覆盖 Eq=False / In=False 分支），并始终加 MISSING
    result: dict[str, tuple[Any, ...]] = {}
    for name, values in dom.items():
        vals = set(values)
        kinds = kind[name]
        if "str" in kinds:
            vals.add(_other_string({v for v in vals if isinstance(v, str)}))
        if "bool" in kinds:
            vals.update({True, False})
        vals.add(MISSING)
        result[name] = tuple(sorted(vals, key=_domain_sort_key))
    return result


def _domain_sort_key(v: Any) -> tuple[int, str]:
    if v is MISSING:
        return (0, "")
    if isinstance(v, bool):
        return (1, f"b{int(v)}")
    if isinstance(v, (int, float)):
        return (2, repr(float(v)))
    return (3, str(v))


def _address_outside(net: ipaddress._BaseNetwork) -> Optional[str]:
    try:
        after = int(net[-1]) + 1
        cand = ipaddress.ip_address(after)
        if cand in net:
            return None
        return str(cand)
    except ValueError:
        return None


def _glob_match_example(pattern: str) -> Optional[str]:
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            continue
        if ch == "?":
            out.append(TAIL_FALLBACK)
        else:
            out.append(ch)
    candidate = "".join(out)
    return candidate if fnmatch.fnmatchcase(candidate, pattern) else None


# ---------------------------------------------------------------------------
# 组装宇宙
# ---------------------------------------------------------------------------
def build_universe(
    scope_prefixes: tuple[str, ...],
    scope_actions: frozenset[str],
    policies: list[Policy],
    *,
    resource_alphabet: list[str],
    configured_principals: list[str],
    include_anonymous: bool,
    max_space_size: int,
) -> Universe:
    tail = validate_alphabet(resource_alphabet)

    # 字母表用于构造区域见证（需要 '/' 与一个非分隔符字符），不要求逐字符
    # 拼出真实前缀——前缀本身以具体字符串给出，区域分割按真实前缀进行。
    regions = build_regions(scope_prefixes,
                            {r.resource_prefix for p in policies
                             for r in p.rules},
                            tail)
    principals = build_principal_domain(configured_principals, include_anonymous, policies)
    actions = build_action_domain(scope_actions, policies)
    attr_domains = build_attribute_domains(policies)

    size = len(regions) * len(principals) * len(actions)
    for vals in attr_domains.values():
        size *= len(vals)
    if size > max_space_size:
        raise SpaceTooLargeError(
            f"受限请求空间大小 {size} 超过上限 {max_space_size}；拒绝静默抽样",
            {"space_size": size, "limit": max_space_size},
        )

    return Universe(
        regions=regions,
        principals=principals,
        actions=actions,
        attribute_domains=attr_domains,
        size=size,
        alphabet=tuple(resource_alphabet),
    )
