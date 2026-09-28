"""安全内核 —— 出站目标校验的唯一裁决者。

一次受保护抓取在 :class:`SecurityKernel` 内被拆成可独立测试的阶段，
每个阶段都向决策链（``HopDecision``）留痕：

    parse → canonicalize → (userinfo 检查) → dns → policy(集合判定)
          → pin 固定 → connect(对端复核) → redirect? → 下一跳重校验

铁律
====
1. **同一策略口径**：URL 主机、DNS 答案、最终连接地址都经过同一个
   规范化（:mod:`addrip`）与同一个策略引擎（:class:`PolicyEngine`）。
2. **每跳重校验**：重定向 Location 被当作全新 URL，从 parse 开始重走
   全部检查；上一跳允许不代表下一跳允许。
3. **固定解析结果连接**：连接器只接收内核选定的 ``IpCandidate``，不再
   解析主机名；连接前后复核对端。
4. **混合集 deny_if_any_forbidden**：解析集合里只要有一个被禁候选，
   整组拒绝，**任何候选都不发起连接**。
5. 内核本身不直接读写审计库；通过 ``AuditSink`` 协议注入，保证内核可
   纯函数式测试。
"""

from __future__ import annotations

import hashlib
import itertools
import os
import threading
import time
from typing import Callable, Protocol

from .contracts import (
    HopDecision,
    HostKind,
    IpCandidate,
    MatchedRule,
    ParsedUrl,
    Verdict,
)
from .errors import (
    InputError,
    KernelError,
    PolicyDeniedError,
    RedirectBudgetError,
    RedirectLoopError,
)
from .net.addrip import canonicalize_ip_literal
from .net.connector import Connector, RawResponse
from .net.resolver import Resolver
from .net.urlparse import join_redirect, parse_and_normalize
from .rules.policy import PolicyEngine

_RUN_COUNTER = itertools.count(1)
_RUN_LOCK = threading.Lock()


def new_run_id() -> str:
    """生成可重放检索的运行编号：时间基 + 进程内自增序号。"""

    with _RUN_LOCK:
        seq = next(_RUN_COUNTER)
    return f"run-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}-{seq:06d}"


class AuditSink(Protocol):
    def record(self, event: dict) -> None: ...


class SecurityKernel:
    def __init__(
        self,
        engine: PolicyEngine,
        resolver: Resolver,
        connector: Connector,
        *,
        audit: AuditSink | None = None,
        run_id_factory: Callable[[], str] = new_run_id,
        connect_timeout: float = 3.0,
    ) -> None:
        self._engine = engine
        self._resolver = resolver
        self._connector = connector
        self._audit = audit
        self._run_id_factory = run_id_factory
        self._connect_timeout = connect_timeout

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def check_only(self, url: str) -> list[HopDecision]:
        """只做解析+DNS+策略判定（不发起任何网络连接）。

        用于测试断言"被禁地址从未被连接"，以及需要决策链但不需要响应体
        的场景。单跳（不跟随重定向——跟随重定向需要真实取 Location）。
        """

        run_id = self._run_id_factory()
        decisions: list[HopDecision] = []
        try:
            parsed = self._stage_parse(url, hop=1, sink=decisions)
            candidates = self._stage_dns(parsed, hop=1, sink=decisions)
            self._stage_policy(parsed, candidates, hop=1, sink=decisions)
        except KernelError as exc:
            self._emit_error(run_id, url, decisions, exc, hop=_current_hop(decisions, 1))
            raise
        finally:
            self._flush_audit(run_id, url, decisions)
        return decisions

    def fetch(self, url: str) -> dict:
        """完整受保护抓取，跟随重定向并逐跳重校验。返回结果字典（含决策链）。"""

        run_id = self._run_id_factory()
        decisions: list[HopDecision] = []
        visited: list[str] = []
        current = url
        limits = self._engine.bundle.limits
        last_response: RawResponse | None = None
        last_pin: IpCandidate | None = None
        result: dict

        try:
            # 最多初始请求 + max_redirects 次重定向请求。
            #
            # 两类失败的显式边界（在"收到 Location、计算出下一跳 URL"后判定）：
            #   * 预算耗尽（resource_exhausted）：已执行的请求数超过
            #     max_redirects（自跳 A→A、紧邻弹跳 A→B→A 都先吃预算）；
            #   * 环（state_conflict）：仍在预算内，但下一跳 URL 在更早的
            #     访问序列中出现过（真闭合循环，如 A→B→C→B）。
            for hop in range(1, limits.max_redirects + 2):
                if hop > limits.max_redirects + 1:
                    raise RedirectBudgetError(
                        f"超过最大重定向次数 {limits.max_redirects}",
                        details={"max": limits.max_redirects, "run_id": run_id,
                                 "visited": visited},
                    )

                parsed = self._stage_parse(current, hop=hop, sink=decisions)
                candidates = self._stage_dns(parsed, hop=hop, sink=decisions)
                pin = self._stage_policy(parsed, candidates, hop=hop, sink=decisions)
                response = self._stage_connect(parsed, pin, hop=hop, sink=decisions)
                last_response, last_pin = response, pin

                if response.status_code in (301, 302, 303, 307, 308) and response.location:
                    nxt = join_redirect(parsed, response.location)
                    decisions[-1] = _with_location(decisions[-1], response.location)
                    visited.append(current.split("#", 1)[0])

                    # 顺序固定：**先预算、后环**。
                    # hop = 已发起的请求数（含本跳）。再跟随一次将是 hop+1 次请求，
                    # 策略允许"初始 + max_redirects 次"，即 hop+1 <= max_redirects+1。
                    if hop + 1 > limits.max_redirects + 1:
                        raise RedirectBudgetError(
                            f"超过最大重定向次数 {limits.max_redirects}",
                            details={"max": limits.max_redirects, "run_id": run_id,
                                     "visited": visited + [nxt.split('#', 1)[0]]},
                        )
                    # 仍有预算余量，再判环：
                    #   * 下一跳 == 当前（自跳 A→A）：视为"未推进"，继续吃预算，
                    #     不在这里报环（否则自跳与循环不可区分）；
                    #   * 下一跳指向更早出现过的*不同* URL：真闭合环。
                    current_key = current.split("#", 1)[0]
                    nxt_key = nxt.split("#", 1)[0]
                    if nxt_key != current_key and nxt_key in visited:
                        origin = visited.index(nxt_key)
                        cycle = visited[origin:] + [nxt_key]
                        raise RedirectLoopError(
                            "检测到重定向环",
                            details={"run_id": run_id, "hop": hop + 1, "cycle": cycle},
                        )
                    current = nxt
                    continue

                result = self._success(run_id, current, decisions, response, pin)
                return result

            raise RedirectBudgetError(
                f"超过最大重定向次数 {limits.max_redirects}",
                details={"max": limits.max_redirects, "run_id": run_id, "visited": visited},
            )
        except KernelError as exc:
            self._emit_error(run_id, url, decisions, exc, hop=_current_hop(decisions, len(visited) or 1))
            category = exc.category.value
            result = {
                "run_id": run_id,
                "final_verdict": Verdict.DENY.value if category == "policy_deny" else category,
                "url": url,
                "hops": _dicts(decisions),
                "status_code": last_response.status_code if last_response else None,
                "connected_peer": list(last_response.peer) if last_response else None,
                "pinned": [last_pin.literal] if last_pin else [],
                "error": exc.to_dict(),
            }
            self._flush_audit(run_id, url, decisions, result=result)
            return result

    # ------------------------------------------------------------------
    # 各阶段（统一口径）
    # ------------------------------------------------------------------
    def _stage_parse(self, url: str, *, hop: int, sink: list[HopDecision]) -> ParsedUrl:
        t0 = time.monotonic()
        try:
            parsed = parse_and_normalize(url, self._engine.bundle.allowed_schemes)
        except InputError:
            raise
        elapsed = (time.monotonic() - t0) * 1000

        # userinfo 检查（与 URL 解析同一入口、策略可配置）
        if parsed.has_userinfo and not self._engine.userinfo_allowed():
            match = MatchedRule(
                rule_id="deny-userinfo",
                action="deny",
                target="authority-userinfo",
                detail="URL 含用户名片段，拒绝以防止 @ 混淆绕过",
            )
            sink.append(
                HopDecision(
                    hop=hop, stage="parse", verdict=Verdict.DENY,
                    reason="userinfo_present", url=url, host=parsed.host,
                    port=parsed.port, matched=match, elapsed_ms=elapsed,
                    note=f"userinfo 证据: {parsed.userinfo_witness}",
                )
            )
            raise PolicyDeniedError(
                "URL 含用户名片段（userinfo），按策略拒绝",
                reason="userinfo_present",
                details={"run_url": url, "hop": hop, "userinfo": parsed.userinfo_witness},
            )

        sink.append(
            HopDecision(
                hop=hop, stage="parse", verdict=Verdict.ALLOW,
                reason="parsed", url=url, host=parsed.host, port=parsed.port,
                elapsed_ms=elapsed,
                note=f"host_kind={parsed.host_kind.value}",
            )
        )
        return parsed

    def _stage_dns(
        self, parsed: ParsedUrl, *, hop: int, sink: list[HopDecision]
    ) -> tuple[IpCandidate, ...]:
        t0 = time.monotonic()
        limits = self._engine.bundle.limits
        if parsed.host_kind in (HostKind.IPV4, HostKind.IPV6):
            # 字面量 IP：规范化结果即候选，不经过 DNS（杜绝 DNS 替换）
            literal, family, _ = canonicalize_ip_literal(
                f"[{parsed.host}]" if parsed.host_kind == HostKind.IPV6 else parsed.host
            )
            candidates = (IpCandidate(literal=literal, family=family, source="literal"),)
            how = "literal_ip"
        else:
            candidates = self._resolver.resolve(parsed.host, limits.max_dns_answers)
            how = f"dns:{self._resolver.sequence_cursor(parsed.host)}th_resolution"
        elapsed = (time.monotonic() - t0) * 1000
        sink.append(
            HopDecision(
                hop=hop, stage="dns", verdict=Verdict.ALLOW, reason=how,
                url=parsed.url, host=parsed.host, port=parsed.port,
                resolved=tuple(c.literal for c in candidates),
                elapsed_ms=elapsed,
            )
        )
        return candidates

    def _stage_policy(
        self,
        parsed: ParsedUrl,
        candidates: tuple[IpCandidate, ...],
        *,
        hop: int,
        sink: list[HopDecision],
    ) -> IpCandidate:
        t0 = time.monotonic()
        verdict, match, per = self._engine.evaluate_set(candidates, parsed.host, parsed.port)
        elapsed = (time.monotonic() - t0) * 1000

        if verdict is Verdict.DENY:
            denied = [c.literal for c, v, _ in per if v is Verdict.DENY]
            allowed_but_not_connected = [c.literal for c, v, _ in per if v is Verdict.ALLOW]
            sink.append(
                HopDecision(
                    hop=hop, stage="policy", verdict=Verdict.DENY,
                    reason=f"deny_if_any_forbidden:{match.rule_id}",
                    url=parsed.url, host=parsed.host, port=parsed.port,
                    resolved=tuple(c.literal for c in candidates),
                    matched=match, elapsed_ms=elapsed,
                    note=(
                        f"被禁候选={denied}; 同组允许但【不会被连接】的候选="
                        f"{allowed_but_not_connected}"
                    ),
                )
            )
            raise PolicyDeniedError(
                f"目标 {parsed.host}:{parsed.port} 的地址集合命中禁止规则 {match.rule_id}",
                reason="forbidden_address_set",
                details={
                    "host": parsed.host,
                    "port": parsed.port,
                    "denied_candidates": denied,
                    "allowed_but_not_connected": allowed_but_not_connected,
                    "rule_id": match.rule_id,
                    "rule_target": match.target,
                    "hop": hop,
                },
            )

        pin = per[0][0]
        sink.append(
            HopDecision(
                hop=hop, stage="policy", verdict=Verdict.ALLOW,
                reason=f"allow:{match.rule_id}", url=parsed.url,
                host=parsed.host, port=parsed.port,
                resolved=tuple(c.literal for c in candidates),
                matched=match, elapsed_ms=elapsed,
                note=f"固定连接 pin={pin.literal}（不再二次解析）",
            )
        )
        return pin

    def _stage_connect(
        self,
        parsed: ParsedUrl,
        pin: IpCandidate,
        *,
        hop: int,
        sink: list[HopDecision],
    ) -> RawResponse:
        t0 = time.monotonic()
        try:
            response = self._connector.fetch(
                parsed, pin,
                timeout=self._connect_timeout,
                max_header_bytes=self._engine.bundle.limits.max_header_bytes,
                max_body_bytes=self._engine.bundle.limits.max_body_bytes,
            )
        except KernelError:
            elapsed = (time.monotonic() - t0) * 1000
            sink.append(
                HopDecision(
                    hop=hop, stage="connect", verdict=Verdict.DENY, reason="connect_failed",
                    url=parsed.url, host=parsed.host, port=parsed.port,
                    resolved=(pin.literal,), peer_checked=(), elapsed_ms=elapsed,
                )
            )
            raise
        elapsed = (time.monotonic() - t0) * 1000
        sink.append(
            HopDecision(
                hop=hop, stage="connect", verdict=Verdict.ALLOW, reason="connected",
                url=parsed.url, host=parsed.host, port=parsed.port,
                resolved=(pin.literal,), peer_checked=(response.peer[0],),
                elapsed_ms=elapsed,
                note=f"对端复核通过: 已批准 pin {pin.literal} == 对端 {response.peer[0]}",
            )
        )
        return response

    # ------------------------------------------------------------------
    # 结果与审计
    # ------------------------------------------------------------------
    def _success(
        self,
        run_id: str,
        final_url: str,
        decisions: list[HopDecision],
        response: RawResponse,
        pin: IpCandidate,
    ) -> dict:
        digest = hashlib.sha256(response.body).hexdigest()
        result = {
            "run_id": run_id,
            "final_verdict": Verdict.ALLOW.value,
            "url": final_url,
            "hops": _dicts(decisions),
            "status_code": response.status_code,
            "body_sha256": digest,
            "body_bytes": len(response.body),
            "connected_peer": list(response.peer),
            "pinned": [pin.literal],
            "error": None,
        }
        self._flush_audit(run_id, final_url, decisions, result=result)
        return result

    def _emit_error(
        self, run_id: str, url: str, decisions: list[HopDecision], exc: KernelError, *, hop: int
    ) -> None:
        # 错误发生时也补一条决策链尾节点（若尚无对应阶段记录）
        stage = {
            "InputError": "parse",
            "PolicyDeniedError": "policy",
            "RedirectLoopError": "redirect",
            "RedirectBudgetError": "redirect",
        }.get(type(exc).__name__, "kernel")
        if not decisions or decisions[-1].stage != stage:
            decisions.append(
                HopDecision(
                    hop=hop, stage=stage, verdict=Verdict.DENY,
                    reason=exc.code, url=url,
                    host=exc.details.get("host", ""), port=exc.details.get("port", 0),
                    note=exc.message,
                )
            )

    def _flush_audit(
        self,
        run_id: str,
        url: str,
        decisions: list[HopDecision],
        *,
        result: dict | None = None,
    ) -> None:
        if self._audit is None:
            return
        payload = result or {
            "run_id": run_id,
            "url": url,
            "hops": _dicts(decisions),
        }
        self._audit.record(payload)


def _current_hop(decisions: list[HopDecision], default: int) -> int:
    return max((d.hop for d in decisions), default=default)


def _with_location(decision: HopDecision, location: str) -> HopDecision:
    # frozen dataclass 替换字段
    return HopDecision(
        hop=decision.hop, stage=decision.stage, verdict=decision.verdict,
        reason=decision.reason, url=decision.url, host=decision.host,
        port=decision.port, resolved=decision.resolved, matched=decision.matched,
        peer_checked=decision.peer_checked, location=location,
        elapsed_ms=decision.elapsed_ms, note=decision.note,
    )


def _dicts(decisions: list[HopDecision]) -> list[dict]:
    return [
        {
            "hop": d.hop, "stage": d.stage, "verdict": d.verdict.value,
            "reason": d.reason, "url": d.url, "host": d.host, "port": d.port,
            "resolved": list(d.resolved),
            "matched": (
                {"rule_id": d.matched.rule_id, "action": d.matched.action,
                 "target": d.matched.target, "detail": d.matched.detail}
                if d.matched else None
            ),
            "peer_checked": list(d.peer_checked),
            "location": d.location,
            "elapsed_ms": round(d.elapsed_ms, 3),
            "note": d.note,
        }
        for d in decisions
    ]
