"""跨模块数据契约。

内核各层（解析 → 规范化 → DNS → 策略 → 连接 → 取页 → 审计）之间只传递
这里定义的不可变对象。禁止层与层之间传裸 ``dict``/``tuple``，字段语义在
此集中说明，便于独立测试与审计回放。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class HostKind(enum.Enum):
    DOMAIN = "domain"
    IPV4 = "ipv4"
    IPV6 = "ipv6"


class Verdict(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    """URL 解析结果。

    * 用户名片段（userinfo）不静默丢弃：``has_userinfo=True`` 时交由策略层
      判 INPUT_ERROR，而不是忽略后按主机放行（http://evil@127.0.0.1 绕过）。
    * ``host_kind`` 在解析期就区分域名与字面量 IP，杜绝"先解析再判断"。
    """

    url: str
    scheme: str
    host: str                 # 已小写、去尾部点（域名字面形式）
    host_kind: HostKind
    port: int                 # 显式或 scheme 默认端口
    has_userinfo: bool
    userinfo_witness: str     # 仅保留掩码形态用于证据，如 "u***@"
    path: str
    query: str
    fragment: str             # fragment 不参与出站，保留仅为审计展示


@dataclass(frozen=True, slots=True)
class IpCandidate:
    """一个待连接的 IP 候选。

    ``literal`` 始终是规范化字符串：IPv4-mapped IPv6 解包为 IPv4 形态
    （::ffff:127.0.0.1 与 127.0.0.1 得到同一个候选）。
    ``family`` 供连接器使用（4/6）。
    """

    literal: str
    family: int
    v6_scope_id: int | None = None
    source: str = "dns"       # "literal" | "dns"
    ordinal: int = 0          # 解析答案序号（重绑定按序返回时用于回放）


@dataclass(frozen=True, slots=True)
class MatchedRule:
    rule_id: str
    action: str               # "allow" | "deny"
    target: str               # 命中的规则目标（cidr / host:port）
    detail: str


@dataclass(frozen=True, slots=True)
class HopDecision:
    """单跳（一次 URL → 连接）的决策链节点。

    这是"输出决策链"的主体：每一步判断都留下可复核的中间状态与理由。
    """

    hop: int
    stage: str                # parse / canonicalize / dns / policy / connect / redirect
    verdict: Verdict
    reason: str
    url: str
    host: str
    port: int
    resolved: tuple[str, ...] = ()          # 规范化后的全部候选 IP
    matched: MatchedRule | None = None
    peer_checked: tuple[str, ...] = ()      # 连接前复核过的对端地址
    location: str | None = None             # 本跳返回的 Location（如有）
    elapsed_ms: float = 0.0
    note: str = ""


@dataclass(frozen=True, slots=True)
class FetchResult:
    """一次受保护抓取的最终结果。"""

    run_id: str
    final_verdict: Verdict
    url: str
    hops: tuple[HopDecision, ...]
    status_code: int | None = None
    body_sha256: str | None = None
    body_bytes: int = 0
    connected_peer: tuple[str, int] | None = None
    pinned: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    def chain_dicts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for h in self.hops:
            out.append(
                {
                    "hop": h.hop,
                    "stage": h.stage,
                    "verdict": h.verdict.value,
                    "reason": h.reason,
                    "url": h.url,
                    "host": h.host,
                    "port": h.port,
                    "resolved": list(h.resolved),
                    "matched": (
                        {
                            "rule_id": h.matched.rule_id,
                            "action": h.matched.action,
                            "target": h.matched.target,
                            "detail": h.matched.detail,
                        }
                        if h.matched
                        else None
                    ),
                    "peer_checked": list(h.peer_checked),
                    "location": h.location,
                    "elapsed_ms": round(h.elapsed_ms, 3),
                    "note": h.note,
                }
            )
        return out


@dataclass(frozen=True, slots=True)
class PolicyLimits:
    max_redirects: int
    max_dns_answers: int
    time_budget_ms: int
    max_header_bytes: int
    max_body_bytes: int


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    action: str                  # "allow" | "deny"
    kind: str                    # "cidr" | "host"
    target: str                  # CIDR 字面量 或 "host:port"
    port: int | None = None      # cidr 规则可限定端口
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    """规则加载器产出的不可变策略快照（状态隔离：一次运行绑定一个快照）。"""

    version: int
    allowed_schemes: frozenset[str]
    default_action: str          # 缺省 "deny"
    mixed_set_mode: str          # "deny_if_any_forbidden"（唯一支持的显式模式）
    deny_userinfo: bool
    limits: PolicyLimits
    rules: tuple[Rule, ...] = field(default_factory=tuple)
    source_path: str = ""
