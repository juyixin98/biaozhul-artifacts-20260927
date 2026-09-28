"""独立朴素预言机（测试专用）。

该模块刻意不 import app.kernel，用另一套直白写法独立重算：
- naive_cache_key：策略声明的维度 + 显式身份分量组成键；
- 期望碰撞：从夹具里的手写 expected_broken/expected_fixed 读取，
  再用独立键分组确认“同键”这一前提成立。

参考答案因此不是被测核心的自证：夹具期望为人工编写，分组逻辑独立实现。
"""
from __future__ import annotations

from typing import Any

from app.parser import canonical_query, parse_vary  # 仅复用无争议的 HTTP 文本解析


def _h(ev: dict, name: str) -> str:
    return ev["request"]["headers"].get(name, "").strip()


def naive_shared_cacheable(policy: dict, ev: dict) -> bool:
    """对夹具场景足够的独立可缓存判断（只依据显式缓存头/状态与身份存储开关）。"""
    headers = ev["response"]["headers"]
    cc = headers.get("Cache-Control", "")
    if "no-store" in cc:
        return False
    state, _ = parse_vary(headers)
    if state == "wildcard" and policy["respect_response_vary"]:
        return False
    if policy["cache_scope"] == "shared":
        if _h(ev, "Authorization") and not policy["allow_storing_authorization_response"]:
            return False
        if "Set-Cookie" in headers and not policy["allow_storing_cookie_response"]:
            return False
    return True


def naive_cache_key(policy: dict, ev: dict) -> tuple:
    """独立键派生：URL 轴 + 生效维度 + 显式身份分量。

    与生产内核刻意分开书写；生效维度独立重算为
    ``policy.vary_headers ∪ (尊重响应时响应 Vary 显式列表)``。
    通配在尊重时由 naive_shared_cacheable 直接判不可缓存，不进入分组。
    """
    parts = [
        policy["cache_scope"],
        ev["request"]["method"].upper(),
        ev["request"]["scheme"],
        ev["request"]["host"],
        ev["request"]["path"],
        canonical_query(ev["request"].get("query", "")),
    ]
    state, names = parse_vary(ev["response"]["headers"])
    effective: list[str] = list(policy["vary_headers"])
    if policy["respect_response_vary"] and state == "explicit":
        for n in names:
            if n not in effective:
                effective.append(n)
    for vh in effective:
        parts.append(("vary", vh, _h(ev, vh)))
    for h in ("Authorization", "Cookie"):
        present = bool(_h(ev, h))
        if not present:
            continue
        if policy["cache_scope"] == "private":
            parts.append(("identity", h, _h(ev, h)))  # 私有缓存隐式按身份隔离
        elif policy.get("include_authorization" if h == "Authorization" else "include_cookie"):
            parts.append(("identity", h, _h(ev, h)))
    return tuple(parts)


def group_by_naive_key(policy: dict, evidence: list[dict]) -> dict[tuple, list[str]]:
    groups: dict[tuple, list[str]] = {}
    for ev in evidence:
        if not naive_shared_cacheable(policy, ev):
            continue
        groups.setdefault(naive_cache_key(policy, ev), []).append(ev["id"])
    return {k: v for k, v in groups.items() if len(v) > 1}


def assert_expected_pair_collides(naive_groups: dict[tuple, list[str]],
                                  expected_pair: list[str]) -> None:
    """手写期望的那对证据，必须在独立键分组里真的落入同一个键。"""
    hit = [ids for ids in naive_groups.values()
           if all(x in ids for x in expected_pair)]
    assert hit, (
        f"独立预言机未观察到 {expected_pair} 同键：{list(naive_groups.values())}"
    )


def assert_expected_pair_separated(naive_groups: dict[tuple, list[str]],
                                   pair: list[str]) -> None:
    for ids in naive_groups.values():
        assert not (pair[0] in ids and pair[1] in ids), (
            f"{pair} 在修复策略下仍同键：{ids}"
        )


def hand_expected(scenario: dict, phase: str) -> dict[str, Any]:
    """读取夹具中人工编写的期望（不由被测内核生成）。"""
    return scenario[f"expected_{phase}"]
