"""跨模块数据与错误契约。

所有模块只通过本文件定义的 dataclass / Enum / 异常通信；
各阶段的失败类别（``FailureKind``）互不重叠，便于测试断言与重放。
"""
from __future__ import annotations

import enum
import itertools
import time
from dataclasses import dataclass, field
from typing import Any

# 运行编号：时间戳 + 进程内单调计数器，保证日志可按编号重放。
_run_counter = itertools.count(1)


def new_run_id() -> str:
    """生成形如 ``run-20260928T113000Z-000007`` 的运行编号。"""
    return f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{next(_run_counter):06d}"


class Verdict(str, enum.Enum):
    """对单个候选目标的最终裁决。"""

    ALLOW = "allow"
    DENY = "deny"


class FailureKind(str, enum.Enum):
    """四类可区分的失败类别（+ 无失败时的 null）。

    映射到 HTTP 状态码由 webapi 负责，内核只产出类别与原因码。
    """

    INPUT_ERROR = "input_error"            # 调用方输入本身不合法
    POLICY_DENIED = "policy_denied"        # 输入合法但策略禁止访问
    STATE_CONFLICT = "state_conflict"      # 运行期状态与策略前提冲突（含重定向环）
    RESOURCE_EXHAUSTED = "resource_exhausted"
    COMPUTATION_FAILED = "computation_failed"  # 下游/IO/密码学等计算失败


class Stage(str, enum.Enum):
    """决策链阶段名（证据 ``stage`` 字段的取值）。"""

    RUN_START = "run_start"
    URL_PARSE = "url_parse"
    DNS_RESOLVE = "dns_resolve"
    IP_CLASSIFY = "ip_classify"
    POLICY = "policy"
    CONNECT = "connect"
    REQUEST_SENT = "request_sent"
    RESPONSE_READ = "response_read"
    REDIRECT = "redirect"
    FINISH = "finish"


class Reason(str, enum.Enum):
    """细粒度原因码 —— 测试按此断言“失败类别 + 原因”。"""

    # input_error
    URL_MALFORMED = "url.malformed"
    URL_BAD_CHARACTER = "url.bad_character"
    SCHEME_UNSUPPORTED = "scheme.unsupported"
    HOST_MISSING = "host.missing"
    HOST_INVALID_LABEL = "host.invalid_label"
    HOST_INTEGER_IP = "host.integer_ip"
    HOST_AMBIGUOUS_NUMERIC = "host.ambiguous_numeric"
    IP_MALFORMED = "ip.malformed"
    PORT_INVALID = "port.invalid"
    USERINFO_FORBIDDEN = "userinfo.forbidden"
    REDIRECT_SCHEME_UNSUPPORTED = "redirect.scheme_unsupported"
    REDIRECT_LOCATION_MISSING = "redirect.location_missing"

    # policy_denied
    IP_BLOCKED = "ip.blocked"
    MIXED_CANDIDATES = "ip.mixed_candidates"
    DEFAULT_DENY = "policy.default_deny"

    # state_conflict
    PIN_MISMATCH = "connect.pin_mismatch"
    REDIRECT_LOOP = "redirect.loop"

    # resource_exhausted
    REDIRECT_LIMIT = "redirect.limit"
    RESPONSE_TOO_LARGE = "response.too_large"
    TIMEOUT_BUDGET = "timeout.budget"

    # computation_failed
    DNS_NAME_NOT_FOUND = "dns.name_not_found"
    DNS_TEMPORARY = "dns.temporary"
    DNS_NO_ADDRESS = "dns.no_address"
    CONNECT_REFUSED = "connect.refused"
    CONNECT_UNREACHABLE = "connect.unreachable"
    CONNECT_RESET = "connect.reset"
    TLS_CERT_VERIFY = "tls.cert_verify"
    TLS_OTHER = "tls.other"
    HTTP_PROTOCOL = "http.protocol"


class SecurityError(Exception):
    """所有内核错误的基类。

    :param kind: 失败类别（四类之一）
    :param reason: :class:`Reason` 细粒度原因码
    :param message: 人类可读说明（不含密钥等敏感数据）
    :param details: 结构化上下文，原样进入审计证据
    """

    kind: FailureKind = FailureKind.COMPUTATION_FAILED

    def __init__(
        self,
        reason: Reason,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason.value,
            "message": self.message,
            "details": self.details,
        }


class InputError(SecurityError):
    kind = FailureKind.INPUT_ERROR


class PolicyDenied(SecurityError):
    kind = FailureKind.POLICY_DENIED


class StateConflict(SecurityError):
    kind = FailureKind.STATE_CONFLICT


class ResourceExhausted(SecurityError):
    kind = FailureKind.RESOURCE_EXHAUSTED


class ComputationFailed(SecurityError):
    kind = FailureKind.COMPUTATION_FAILED


@dataclass(frozen=True)
class ParsedTarget:
    """``urlparse`` 阶段的输出契约。"""

    url: str                  # 规范化后的 URL（ASCII、显式端口、无 fragment）
    scheme: str               # 小写，仅 http/https
    host: str                 # 规范化主机：DNS 名（IDNA ASCII、去尾点）或 IP 字面量
    host_kind: str            # "dns" | "ipv4" | "ipv6"
    port: int                 # 显式或默认端口
    has_userinfo: bool
    path: str                 # 以 "/" 开头
    raw_host: str             # 规范化前主机（证据用）
    normalized_for_lookup: str  # 送 DNS / connect 的主机（IP 字面量为规范压缩形式）


@dataclass(frozen=True)
class ResolvedAddress:
    """一个解析候选地址。

    ``canonical_ip`` 永远是连接/分类使用的形态：
    IPv4-mapped/兼容 IPv6 在此解包为 IPv4（``unwrapped_from`` 记录原形态）。
    """

    ip: str
    family: str               # "ipv4" | "ipv6"
    canonical_ip: str
    tags: frozenset[str] = field(default_factory=frozenset)
    unwrapped_from: str | None = None  # 若由 ::ffff:1.2.3.4 解包，记录原始字面量


@dataclass(frozen=True)
class ResolvedTarget:
    """``dns_resolve`` 阶段输出：一次性解析快照（pinning 的依据）。"""

    host: str
    port: int
    addresses: tuple[ResolvedAddress, ...]
    source: str               # "literal" | "controlled_dns"
    lookup_attempts: int      # 解析器被调用次数（重绑定测试据此断言只解析一次）


@dataclass
class Evidence:
    """决策链上的一条证据（可序列化为 JSON）。"""

    stage: str
    verdict: str | None       # allow | deny | None（中性事件）
    reason: str | None
    detail: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "stage": self.stage,
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass
class HopRecord:
    """一跳（一次 URL 尝试）的完整记录。"""

    hop: int
    url: str
    parsed: dict[str, Any]
    resolved: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    outcome: str = "pending"  # allowed | denied | error | redirected


@dataclass
class RunResult:
    """一次 ``guard.fetch`` 的完整结果 —— 审计与重放的主对象。"""

    run_id: str
    requested_url: str
    verdict: str                      # Verdict 终态：allow / deny
    status: str                       # completed | denied | error
    start_ts: float
    end_ts: float = 0.0
    hops: list[HopRecord] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    failure: dict[str, Any] | None = None
    response: dict[str, Any] | None = None
    policy_file: str | None = None
    max_redirects: int = 5
    max_bytes: int = 1 << 20
    timeout_s: float = 5.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "requested_url": self.requested_url,
            "verdict": self.verdict,
            "status": self.status,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "duration_ms": round((self.end_ts - self.start_ts) * 1000, 3),
            "hops": [_hop_dict(h) for h in self.hops],
            "decision_chain": [e.to_dict() for e in self.evidence],
            "failure": self.failure,
            "response": self.response,
            "policy_file": self.policy_file,
            "limits": {
                "max_redirects": self.max_redirects,
                "max_bytes": self.max_bytes,
                "timeout_s": self.timeout_s,
            },
        }


def _hop_dict(h: HopRecord) -> dict[str, Any]:
    return {
        "hop": h.hop,
        "url": h.url,
        "parsed": h.parsed,
        "resolved": h.resolved,
        "policy": h.policy,
        "attempts": h.attempts,
        "outcome": h.outcome,
    }
