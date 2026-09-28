"""有序规则策略：规则/证据解析与求值。

策略文件（JSON）结构见 fixtures/policy/rules.json：

.. code-block:: json

    {
      "version": 1,
      "default": "deny",
      "rules": [
        {"id": "demo-local-http", "action": "allow", "host": "demo.local",
         "scheme": "http", "port": 0, "note": "..."},
        {"id": "block-internal", "action": "deny", "tag_any": ["loopback"]}
      ]
    }

求值语义（“混合允许与禁止地址集合”的明确规则）：

1. 规则**有序**，自上而下首条命中者生效（first-match-wins）；
2. 对 DNS 名解析出的**每个**候选地址分别求规则；
3. 只有**全部**候选地址同一条 fetch 都得到 allow，该跳才允许连接；
   任意候选命中 deny 或无规则（default deny）→ 整跳拒绝，原因码
   ``ip.mixed_candidates``（集合同时含允许/禁止）或 ``policy.default_deny``；
4. 规则匹配维度可自由组合（scheme/host/port/tag_any），未列出的维度视为通配；
   ``port`` 为 ``null`` 表示任意端口；
5. 运行期 grant（演示用的精确放行）与文件规则同构，grant 排在文件规则之前。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import InputError, ParsedTarget, Reason, ResolvedAddress

_ACTIONS = ("allow", "deny")


@dataclass(frozen=True)
class Rule:
    id: str
    action: str
    scheme: str | None = None
    host: str | None = None
    port: int | None = None          # None = 任意端口
    tag_any: tuple[str, ...] = ()
    note: str = ""

    def matches(self, target: ParsedTarget, addr: ResolvedAddress) -> bool:
        if self.scheme is not None and self.scheme != target.scheme:
            return False
        if self.host is not None and self.host != target.host:
            return False
        if self.port is not None and self.port != target.port:
            return False
        if self.tag_any and not (set(self.tag_any) & set(addr.tags)):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
            "tag_any": list(self.tag_any),
            "note": self.note,
        }


@dataclass(frozen=True)
class AddressDecision:
    """单个候选地址的策略裁决（证据用）。"""

    ip: str
    family: str
    tags: tuple[str, ...]
    action: str
    rule_id: str
    unwrapped_from: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "family": self.family,
            "tags": list(self.tags),
            "action": self.action,
            "rule_id": self.rule_id,
            "unwrapped_from": self.unwrapped_from,
        }


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: Reason | None
    decisions: tuple[AddressDecision, ...]
    chosen: AddressDecision | None    # 允许时实际将连接的候选（取首个 allow）

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": None if self.reason is None else self.reason.value,
            "addresses": [d.to_dict() for d in self.decisions],
            "chosen": None if self.chosen is None else self.chosen.to_dict(),
        }


class Policy:
    def __init__(self, rules: list[Rule], *, source: str, default: str = "deny") -> None:
        if default not in _ACTIONS:
            raise ValueError(f"default 必须是 allow/deny，得到 {default!r}")
        self.rules = tuple(rules)
        self.default = default
        self.source = source

    @classmethod
    def from_file(cls, path: str | Path) -> "Policy":
        p = Path(path)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise InputError(Reason.URL_MALFORMED, f"策略文件不存在: {p}", {"path": str(p)}) from exc
        except json.JSONDecodeError as exc:
            raise InputError(
                Reason.URL_MALFORMED,
                f"策略文件不是合法 JSON: {exc}",
                {"path": str(p), "line": exc.lineno, "col": exc.colno},
            ) from exc
        rules = _parse_rules(data.get("rules", []))
        default = data.get("default", "deny")
        return cls(rules, source=str(p), default=default)

    def with_grants(self, grants: list[dict[str, Any]] | None) -> "Policy":
        """返回在文件规则**之前**插入运行期 grant 的新策略（状态隔离）。"""
        if not grants:
            return self
        grant_rules = _parse_rules(grants, id_prefix="grant")
        return Policy([*grant_rules, *self.rules], source=self.source + "+grants", default=self.default)

    def evaluate(self, target: ParsedTarget, addresses: tuple[ResolvedAddress, ...]) -> PolicyDecision:
        decisions = tuple(self._decide_one(target, a) for a in addresses)
        allows = [d for d in decisions if d.action == "allow"]
        denies = [d for d in decisions if d.action == "deny"]

        if not decisions:
            # 空集合不应发生（resolver 保证），保底 default deny
            return PolicyDecision(False, Reason.DEFAULT_DENY, (), None)

        if allows and not denies:
            chosen = allows[0]
            return PolicyDecision(True, None, decisions, chosen)
        if allows and denies:
            return PolicyDecision(False, Reason.MIXED_CANDIDATES, decisions, None)
        # 全 deny
        explicit = [d for d in denies if d.rule_id != "default"]
        if explicit:
            return PolicyDecision(False, Reason.IP_BLOCKED, decisions, None)
        return PolicyDecision(False, Reason.DEFAULT_DENY, decisions, None)

    def _decide_one(self, target: ParsedTarget, addr: ResolvedAddress) -> AddressDecision:
        for rule in self.rules:
            if rule.matches(target, addr):
                return AddressDecision(
                    ip=addr.canonical_ip,
                    family=addr.family,
                    tags=tuple(sorted(addr.tags)),
                    action=rule.action,
                    rule_id=rule.id,
                    unwrapped_from=addr.unwrapped_from,
                )
        return AddressDecision(
            ip=addr.canonical_ip,
            family=addr.family,
            tags=tuple(sorted(addr.tags)),
            action=self.default,
            rule_id="default",
            unwrapped_from=addr.unwrapped_from,
        )


def _parse_rules(raw: list[dict[str, Any]], *, id_prefix: str = "rule") -> list[Rule]:
    if not isinstance(raw, list):
        raise InputError(Reason.URL_MALFORMED, "策略 rules 必须是数组", {"got": type(raw).__name__})
    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InputError(Reason.URL_MALFORMED, f"规则 #{i} 不是对象", {"index": i})
        rid = str(item.get("id") or f"{id_prefix}-{i}")
        if rid in seen_ids:
            raise InputError(Reason.URL_MALFORMED, f"规则 id 重复: {rid}", {"id": rid})
        seen_ids.add(rid)
        action = item.get("action")
        if action not in _ACTIONS:
            raise InputError(
                Reason.URL_MALFORMED,
                f"规则 {rid} 的 action 必须是 allow/deny",
                {"id": rid, "action": action},
            )
        tag_any = item.get("tag_any") or []
        if not isinstance(tag_any, list) or not all(isinstance(t, str) for t in tag_any):
            raise InputError(
                Reason.URL_MALFORMED,
                f"规则 {rid} 的 tag_any 必须是字符串数组",
                {"id": rid},
            )
        port = item.get("port", None)
        if port is not None and (not isinstance(port, int) or not 0 <= port <= 65535):
            raise InputError(
                Reason.URL_MALFORMED,
                f"规则 {rid} 的 port 必须是 0-65535 的整数或 null",
                {"id": rid, "port": port},
            )
        rules.append(
            Rule(
                id=rid,
                action=action,
                scheme=item.get("scheme"),
                host=item.get("host"),
                port=port,
                tag_any=tuple(tag_any),
                note=str(item.get("note", "")),
            )
        )
    return rules
