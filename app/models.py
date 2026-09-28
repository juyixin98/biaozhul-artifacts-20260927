"""API 与内核之间的数据契约（Pydantic 模型）。

字段命名刻意贴近 HTTP 语义（authorization/cookie/vary_*），
隐私相关原始值（Authorization、Cookie）在存储层加密，内核只看归一化后的值。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .parser import normalize_header_name, parse_vary

CacheScope = Literal["shared", "private"]
RunStatus = Literal["open", "sealed"]
EvidenceSource = Literal["observation", "synthetic_fixture"]


class RequestEvidence(BaseModel):
    method: str = Field(description="HTTP 方法，原样保留大小写但分析时归一为大写")
    scheme: Literal["http", "https"]
    host: str = Field(min_length=1)
    path: str = Field(min_length=1, description="路径，必须以 / 开头")
    query: str = Field(default="", description="原始 query 串（不含 ?）")
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("method")
    @classmethod
    def _method_upper(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("method 不能为空")
        return v.upper()

    @field_validator("path")
    @classmethod
    def _path_slash(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("path 必须以 / 开头")
        return v

    @field_validator("headers")
    @classmethod
    def _normalize_header_keys(cls, v: dict[str, str]) -> dict[str, str]:
        norm: dict[str, str] = {}
        for k, val in v.items():
            norm[normalize_header_name(k)] = val
        return norm


class ResponseEvidence(BaseModel):
    status: int = Field(ge=100, le=599)
    headers: dict[str, str] = Field(default_factory=dict)
    body_sha256: str = Field(description="调用方对响应体（原始字节）计算的 SHA-256（hex）")
    body: str = Field(default="", description="响应体；body_encoding=base64 时为原始字节的 base64")
    body_encoding: Literal["utf-8", "base64"] = "utf-8"
    note: str = ""

    @field_validator("headers")
    @classmethod
    def _normalize_header_keys(cls, v: dict[str, str]) -> dict[str, str]:
        norm: dict[str, str] = {}
        for k, val in v.items():
            norm[normalize_header_name(k)] = val
        return norm

    @field_validator("body_sha256")
    @classmethod
    def _hex(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("body_sha256 必须是 64 位十六进制")
        return v


class Evidence(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    source: EvidenceSource = "observation"
    request: RequestEvidence
    response: ResponseEvidence
    note: str = ""


class EvidenceBatch(BaseModel):
    evidence: list[Evidence]


class Policy(BaseModel):
    """被审计的缓存策略声明。

    vary_headers:      策略声明纳入缓存键的请求头（等价于响应 Vary 白名单）
    include_authorization: 策略是否把 Authorization 纳入键
    include_cookie:        策略是否把 Cookie 纳入键
    allow_storing_authorization_response: 共享缓存是否允许存带授权的响应
    allow_storing_cookie_response:        共享缓存是否允许存 Set-Cookie 响应
    respect_response_vary: 是否采信响应自身的 Vary（修复键的关键开关）
    vary_wildcard_mode: 响应 Vary: * 时的策略：forbid 存储 / uncacheable 放行不缓存
    """

    name: str = Field(min_length=1, max_length=200)
    cache_scope: CacheScope
    vary_headers: list[str] = Field(default_factory=list)
    include_authorization: bool = False
    include_cookie: bool = False
    allow_storing_authorization_response: bool = False
    allow_storing_cookie_response: bool = False
    respect_response_vary: bool = True
    vary_wildcard_mode: Literal["forbid", "uncacheable"] = "forbid"

    @field_validator("vary_headers")
    @classmethod
    def _normalize_vary(cls, v: list[str]) -> list[str]:
        names = [normalize_header_name(h) for h in v if h.strip()]
        if any(n == "*" for n in names):
            raise ValueError("vary_headers 不允许包含通配符 *；通配是响应 Vary 的特殊语义")
        # 去重保序
        seen: set[str] = set()
        out: list[str] = []
        for n in names:
            if n not in seen:
                seen.add(n)
                out.append(n)
        return out

    def safe_int(self) -> int:
        """供测试与内核使用的简单结构版本号。"""
        return len(self.vary_headers)


class CreateRunRequest(BaseModel):
    run_id: str | None = Field(default=None, max_length=64)
    label: str = Field(default="", max_length=200)


class RunView(BaseModel):
    run_id: str
    label: str
    status: RunStatus
    evidence_count: int
    has_policy: bool
    analyzed: bool
    created_at: str


class WitnessPair(BaseModel):
    witness_id: str
    evidence_a: str
    evidence_b: str
    dimension: str
    reason: str
    severity: Literal["critical", "high", "medium"]
    request_a: dict
    request_b: dict
    key_components_a: dict
    key_components_b: dict
    response_body_a_sha256: str
    response_body_b_sha256: str


class FindingView(BaseModel):
    category: str
    dimension: str
    reason: str
    severity: str
    pair: WitnessPair


class AnalysisView(BaseModel):
    run_id: str
    policy_name: str
    cache_scope: str
    analyzed_evidence: int
    collision_groups: int
    findings: list[FindingView]
    cacheable_decisions: dict[str, bool]
    effective_vary: dict[str, list[str]]
    response_vary_state: dict[str, str]
    derived_keys: dict[str, dict]
    decision_rationale: list[str]


class RemediationView(BaseModel):
    run_id: str
    broken_policy_name: str
    fixed_policy: Policy
    before: AnalysisView
    after: AnalysisView
    cleared_witness_ids: list[str]
    residual_witness_ids: list[str]
    collision_gone: bool


class EventView(BaseModel):
    seq: int
    event_type: str
    timestamp: str
    payload: dict
    prev_hash: str
    entry_hash: str


class VerifyView(BaseModel):
    run_id: str
    ok: bool
    entries: int
    first_mismatch_seq: int | None = None
