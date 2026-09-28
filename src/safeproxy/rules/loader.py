"""策略规则加载器 —— 规则/证据解析边界。

输入是本地 JSON 策略文件（合成夹具），输出是不可变 :class:`PolicyBundle`。

* 严格 schema 校验：未知字段、错误类型、错误 action 一律 PolicyFileError；
* ``${ENV:DEFAULT}`` 插值，让夹具可以用固定/环境端口而不硬编码；
* CIDR 用标准库预解析，加载期失败而非运行期才暴露；
* 加载器不做任何安全判定，只负责"文件 → 合法快照"。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from typing import Any

from ..contracts import PolicyBundle, PolicyLimits, Rule
from ..errors import PolicyFileError

_INTERP_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")
_VALID_ACTIONS = {"allow", "deny"}
_VALID_RULE_KINDS = {"cidr", "host"}
_VALID_MIXED = {"deny_if_any_forbidden"}
_REQUIRED_TOP = ("version", "allowed_schemes", "default_action", "mixed_address_set", "limits", "rules")
_ALLOWED_TOP = frozenset(_REQUIRED_TOP) | {"deny_userinfo"}


def load_policy(path: str) -> PolicyBundle:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except OSError as exc:
        raise PolicyFileError(f"无法读取策略文件 {path}: {exc}", details={"path": path}) from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise PolicyFileError(
            f"策略文件 JSON 非法: {exc}",
            details={"path": path, "line": exc.lineno, "col": exc.colno},
        ) from exc

    if not isinstance(data, dict):
        raise PolicyFileError("策略文件顶层必须是对象", details={"path": path})

    missing = [k for k in _REQUIRED_TOP if k not in data]
    if missing:
        raise PolicyFileError(f"策略缺少必需字段: {missing}", details={"missing": missing, "path": path})
    extras = [k for k in data if k not in _ALLOWED_TOP]
    if extras:
        raise PolicyFileError(f"策略含未知字段: {extras}", details={"extras": extras, "path": path})

    version = data["version"]
    if version != 1:
        raise PolicyFileError(f"不支持的策略版本 {version}（仅支持 1）", details={"version": version})

    schemes = data["allowed_schemes"]
    if not isinstance(schemes, list) or not schemes or not all(isinstance(s, str) for s in schemes):
        raise PolicyFileError("allowed_schemes 必须是非空字符串列表")
    allowed = frozenset(s.lower() for s in schemes)
    if not allowed <= {"http", "https"}:
        raise PolicyFileError(f"allowed_schemes 含未知 scheme: {sorted(allowed)}")

    default = data["default_action"]
    if default not in _VALID_ACTIONS:
        raise PolicyFileError(f"default_action 必须是 allow/deny，得到 {default!r}")

    mixed = data["mixed_address_set"]
    if mixed not in _VALID_MIXED:
        raise PolicyFileError(
            f"mixed_address_set 必须显式声明为 {sorted(_VALID_MIXED)}（禁止隐式行为）",
            details={"value": mixed},
        )

    deny_userinfo = bool(data.get("deny_userinfo", True))

    limits = _parse_limits(data["limits"])
    rules = _parse_rules(data["rules"])

    return PolicyBundle(
        version=version,
        allowed_schemes=allowed,
        default_action=default,
        mixed_set_mode=mixed,
        deny_userinfo=deny_userinfo,
        limits=limits,
        rules=rules,
        source_path=os.path.abspath(path),
    )


def _parse_limits(raw: Any) -> PolicyLimits:
    if not isinstance(raw, dict):
        raise PolicyFileError("limits 必须是对象")
    allowed_keys = {"max_redirects", "max_dns_answers", "time_budget_ms",
                    "max_header_bytes", "max_body_bytes"}
    extras = set(raw) - allowed_keys
    if extras:
        raise PolicyFileError(f"limits 含未知字段: {sorted(extras)}")
    values: dict[str, int] = {}
    for key in allowed_keys:
        if key not in raw:
            raise PolicyFileError(f"limits 缺少 {key}")
        v = raw[key]
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise PolicyFileError(f"limits.{key} 必须是正整数，得到 {v!r}")
        values[key] = v
    return PolicyLimits(**values)


def _parse_rules(raw: Any) -> tuple[Rule, ...]:
    if not isinstance(raw, list) or not raw:
        raise PolicyFileError("rules 必须是非空列表")
    seen_ids: set[str] = set()
    out: list[Rule] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise PolicyFileError(f"rules[{i}] 必须是对象")
        where = f"rules[{i}]"
        for key in ("id", "action", "kind", "target"):
            if key not in item:
                raise PolicyFileError(f"{where} 缺少 {key}")
        extras = set(item) - {"id", "action", "kind", "target", "port", "rationale"}
        if extras:
            raise PolicyFileError(f"{where} 含未知字段: {sorted(extras)}")

        rid = item["id"]
        if not isinstance(rid, str) or not re.fullmatch(r"[a-z0-9_-]+", rid):
            raise PolicyFileError(f"{where}.id 非法: {rid!r}")
        if rid in seen_ids:
            raise PolicyFileError(f"规则 id 重复: {rid}")
        seen_ids.add(rid)

        action = item["action"]
        if action not in _VALID_ACTIONS:
            raise PolicyFileError(f"{where}.action 必须是 allow/deny")
        kind = item["kind"]
        if kind not in _VALID_RULE_KINDS:
            raise PolicyFileError(f"{where}.kind 必须是 cidr/host")

        target = _interpolate(str(item["target"]))
        port = item.get("port")
        if port is not None and (not isinstance(port, int) or isinstance(port, bool)
                                 or not 1 <= port <= 65535):
            raise PolicyFileError(f"{where}.port 必须是 1-65535 的整数")

        rationale = str(item.get("rationale", ""))
        if kind == "cidr":
            try:
                ipaddress.ip_network(target, strict=False)
            except ValueError as exc:
                raise PolicyFileError(
                    f"{where}.target 不是合法 CIDR: {target!r}",
                    details={"rule": rid, "target": target},
                ) from exc
        else:  # host
            if not re.fullmatch(r"[a-z0-9.-]+|\[[0-9a-f:]+\]", target):
                raise PolicyFileError(f"{where}.target 主机形式非法: {target!r}")

        out.append(Rule(rule_id=rid, action=action, kind=kind, target=target,
                        port=port, rationale=rationale))
    return tuple(out)


def _interpolate(text: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        return os.environ.get(name, default if default is not None else "")

    return _INTERP_RE.sub(repl, text)
