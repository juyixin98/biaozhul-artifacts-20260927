"""安全内核：把 解析 → DNS → 分类 → 策略 → 固定连接 → HTTP → 重定向重校验
串成一条可审计的决策链。

不变式：

1. **同一策略口径**：URL 规范化主机、DNS 候选、实际连接 IP 都来自
   :mod:`app.urlparse` / :mod:`app.ipclass` 的同一规范化结果；
2. **一次性解析**：每跳对主机恰好调用一次 resolver，快照在该跳内固定；
3. **重定向逐跳重校验**：每个 Location 都重新走 解析/DNS/分类/策略 全部步骤；
4. **环 / 跳数**：规范化 URL 重复 → ``STATE_CONFLICT/redirect.loop``；
   超过 max_redirects → ``RESOURCE_EXHAUSTED/redirect.limit``；
5. 任何失败都终结该 run，并以明确的 FailureKind + reason 落审计。
"""
from __future__ import annotations

import ssl
import time
from dataclasses import dataclass
from typing import Any

from . import httpclient
from . import urlparse as url_mod
from .connector import Connector, PinnedConnector, RecordingConnector
from .contracts import (
    Evidence,
    FailureKind,
    HopRecord,
    InputError,
    PolicyDenied,
    Reason,
    ResourceExhausted,
    RunResult,
    SecurityError,
    Stage,
    StateConflict,
    Verdict,
    new_run_id,
)
from .policy import Policy
from .resolver import ControlledResolver

_FAILURE_STATUS = {
    FailureKind.INPUT_ERROR: "error",
    FailureKind.COMPUTATION_FAILED: "error",
    FailureKind.RESOURCE_EXHAUSTED: "error",
    FailureKind.STATE_CONFLICT: "error",
    FailureKind.POLICY_DENIED: "denied",
}


@dataclass
class HopOutcome:
    """``_one_hop`` 的返回值（避免在 HopRecord 上挂动态属性）。"""

    hop: HopRecord
    kind: str  # "response" | "redirected"
    redirect_location: str | None = None
    response_dict: dict[str, Any] | None = None


class AuditSink:  # pragma: no cover - 协议定义（audit.py 实现）
    def begin_run(self, result: RunResult) -> None: ...
    def finish_run(self, result: RunResult) -> None: ...


class GuardKernel:
    def __init__(
        self,
        *,
        resolver: ControlledResolver,
        policy: Policy,
        connector: Connector | None = None,
        max_redirects: int = 5,
        max_bytes: int = 1 << 20,
        timeout_s: float = 5.0,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self.resolver = resolver
        self.policy = policy
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        self.tls_context = tls_context
        # RecordingConnector 让每次 fetch 的连接尝试可被测试逐条断言。
        # 每次 fetch() 重置，保证运行间状态隔离。
        self._connector_factory = lambda: RecordingConnector(connector or PinnedConnector())

    # -- 公开入口 ----------------------------------------------------------

    def fetch(
        self,
        url: str,
        *,
        audit_sink: AuditSink | None = None,
        run_id: str | None = None,
    ) -> RunResult:
        result = RunResult(
            run_id=run_id or new_run_id(),
            requested_url=url,
            verdict=Verdict.DENY.value,  # 默认拒绝，成功完成才置 allow
            status="running",
            start_ts=time.time(),
            policy_file=self.policy.source,
            max_redirects=self.max_redirects,
            max_bytes=self.max_bytes,
            timeout_s=self.timeout_s,
        )
        seq = 0
        sink = audit_sink

        def emit(stage: Stage, verdict: Verdict | None, reason: Reason | str | None,
                 detail: dict[str, Any]) -> None:
            nonlocal seq
            seq += 1
            result.evidence.append(Evidence(
                stage=stage.value,
                verdict=None if verdict is None else verdict.value,
                reason=None if reason is None else (reason.value if isinstance(reason, Reason) else reason),
                detail=dict(detail),
                seq=seq,
            ))

        try:
            if sink is not None:
                sink.begin_run(result)

            emit(Stage.RUN_START, None, None, {
                "requested_url": url,
                "policy_source": self.policy.source,
                "resolver_source": self.resolver.source,
                "max_redirects": self.max_redirects,
                "max_bytes": self.max_bytes,
                "timeout_s": self.timeout_s,
            })

            current_url = url
            seen: set[str] = set()
            response_dict: dict[str, Any] | None = None

            # 最多发起 max_redirects+1 个请求；第 1 跳不算重定向
            for request_index in range(self.max_redirects + 1):
                outcome = self._one_hop(
                    result, current_url, hop_index=request_index + 1, emit=emit
                )

                if outcome.kind == "response":
                    response_dict = outcome.response_dict
                    result.response = response_dict
                    break

                # 重定向
                assert outcome.redirect_location is not None
                location = outcome.redirect_location
                if location in seen:
                    raise StateConflict(
                        Reason.REDIRECT_LOOP,
                        "重定向回到此前已访问的规范化 URL",
                        {"repeated_url": location, "visited": sorted(seen)},
                    )
                seen.add(current_url)
                emit(Stage.REDIRECT, None, None, {
                    "hop": request_index + 1,
                    "from": current_url,
                    "to": location,
                })
                current_url = location
            else:
                raise ResourceExhausted(
                    Reason.REDIRECT_LIMIT,
                    f"重定向跳数超过上限 {self.max_redirects}",
                    {"max_redirects": self.max_redirects, "next_url": current_url},
                )

            result.verdict = Verdict.ALLOW.value
            result.status = "completed"
            emit(Stage.FINISH, Verdict.ALLOW, None, {
                "status_code": response_dict.get("status") if response_dict else None,
                "requests": len(result.hops),
            })
        except SecurityError as exc:
            result.failure = exc.to_dict()
            result.verdict = Verdict.DENY.value
            result.status = _FAILURE_STATUS.get(exc.kind, "error")
            # 若异常发生在某跳内部（该 hop 已挂入 result），把终态写回，
            # 否则审计里会留下 outcome=pending 的误导记录。
            if result.hops and result.hops[-1].outcome == "pending":
                result.hops[-1].outcome = (
                    "denied" if exc.kind == FailureKind.POLICY_DENIED else "error"
                )
            emit(Stage.FINISH, Verdict.DENY, exc.reason, exc.details)
        except Exception as exc:  # 未预期异常归 computation_failed，绝不静默放行
            result.failure = {
                "kind": FailureKind.COMPUTATION_FAILED.value,
                "reason": "internal.unexpected",
                "message": f"{type(exc).__name__}: {exc}",
                "details": {},
            }
            result.verdict = Verdict.DENY.value
            result.status = "error"
            if result.hops and result.hops[-1].outcome == "pending":
                result.hops[-1].outcome = "error"
            emit(Stage.FINISH, Verdict.DENY, "internal.unexpected",
                 {"exception": type(exc).__name__})
        finally:
            result.end_ts = time.time()
            if sink is not None:
                sink.finish_run(result)
        return result

    # -- 单跳 --------------------------------------------------------------

    def _one_hop(
        self,
        result: RunResult,
        raw_url: str,
        *,
        hop_index: int,
        emit,
    ) -> HopOutcome:
        parsed = url_mod.parse_target(raw_url)
        hop = HopRecord(hop=hop_index, url=parsed.url, parsed={
            "scheme": parsed.scheme,
            "host": parsed.host,
            "host_kind": parsed.host_kind,
            "port": parsed.port,
            "has_userinfo": parsed.has_userinfo,
            "raw_host": parsed.raw_host,
            "normalized_url": parsed.url,
        })
        emit(Stage.URL_PARSE, Verdict.ALLOW, None, {
            "hop": hop_index,
            "normalized_url": parsed.url,
            "host": parsed.host,
            "host_kind": parsed.host_kind,
            "port": parsed.port,
        })

        resolved = self.resolver.resolve(
            parsed.normalized_for_lookup, parsed.port, host_kind=parsed.host_kind
        )
        hop.resolved = {
            "source": resolved.source,
            "lookup_attempts": resolved.lookup_attempts,
            "addresses": [
                {
                    "ip": a.ip,
                    "family": a.family,
                    "canonical_ip": a.canonical_ip,
                    "tags": sorted(a.tags),
                    "unwrapped_from": a.unwrapped_from,
                }
                for a in resolved.addresses
            ],
        }
        result.hops.append(hop)
        emit(Stage.DNS_RESOLVE, None, None, {**hop.resolved, "hop": hop_index})
        emit(Stage.IP_CLASSIFY, None, None, {
            "hop": hop_index,
            "addresses": [
                {"ip": a.canonical_ip, "tags": sorted(a.tags),
                 "unwrapped_from": a.unwrapped_from}
                for a in resolved.addresses
            ],
        })

        decision = self.policy.evaluate(parsed, resolved.addresses)
        hop.policy = decision.to_dict()
        if not decision.allowed:
            assert decision.reason is not None
            emit(Stage.POLICY, Verdict.DENY, decision.reason, {
                "hop": hop_index,
                "decision": decision.to_dict(),
            })
            raise PolicyDenied(
                decision.reason,
                f"策略拒绝 {parsed.url}: {decision.reason.value}",
                {"hop": hop_index, "decision": decision.to_dict()},
            )

        assert decision.chosen is not None
        emit(Stage.POLICY, Verdict.ALLOW, None, {
            "hop": hop_index,
            "decision": decision.to_dict(),
        })

        # 每个 fetch 使用全新 RecordingConnector；固定到决策选定的单个 IP。
        # 本跳之后 resolver 即使被再次触发，其结果也无人消费。
        connector = self._connector_factory()
        try:
            conn = connector.connect(
                parsed, decision.chosen, timeout=self.timeout_s, tls_context=self.tls_context
            )
        except SecurityError:
            # 连接阶段失败也要留下“尝试过哪个 IP”的可审计记录
            hop.attempts.extend(a.__dict__ for a in connector.attempts)
            emit(Stage.CONNECT, Verdict.DENY, None, {
                "hop": hop_index,
                "pinned_ip": decision.chosen.ip,
                "recorded_attempts": [a.__dict__ for a in connector.attempts],
            })
            raise
        hop.attempts.append({
            "ip": decision.chosen.ip,
            "port": parsed.port,
            "transport": conn.transport,
            "peer_ip": conn.peer_ip,
            "result": "opened",
        })
        emit(Stage.CONNECT, Verdict.ALLOW, None, {
            "hop": hop_index,
            "pinned_ip": decision.chosen.ip,
            "peer_ip": conn.peer_ip,
            "transport": conn.transport,
            "rule_id": decision.chosen.rule_id,
            "recorded_attempts": [a.__dict__ for a in connector.attempts],
        })
        try:
            httpclient.write_request(conn, parsed)
            emit(Stage.REQUEST_SENT, None, None, {
                "hop": hop_index, "method": "GET", "host_header": parsed.host,
            })
            response = httpclient.read_response(conn, max_bytes=self.max_bytes)
        finally:
            conn.close()

        hop.attempts[-1]["result"] = "completed"
        emit(Stage.RESPONSE_READ, None, None, {
            "hop": hop_index,
            "status": response.status,
            "body_bytes": len(response.body),
            "body_truncated": response.body_truncated,
            "is_redirect": response.is_redirect,
        })

        if response.is_redirect:
            location = response.location
            if location is None:
                raise InputError(
                    Reason.REDIRECT_LOCATION_MISSING,
                    f"{response.status} 响应缺少 Location 头",
                    {"hop": hop_index},
                )
            joined = url_mod.join_redirect(parsed, location)
            lowered = joined.lower()
            if not (lowered.startswith("http://") or lowered.startswith("https://")):
                raise InputError(
                    Reason.REDIRECT_SCHEME_UNSUPPORTED,
                    f"重定向到非 http(s) 方案: {joined}",
                    {"hop": hop_index, "location": joined},
                )
            hop.outcome = "redirected"
            return HopOutcome(hop=hop, kind="redirected", redirect_location=joined)

        hop.outcome = "allowed"
        return HopOutcome(hop=hop, kind="response", response_dict=response.to_dict())
