"""接受 / 拒绝 / 无法判定三态诊断。

每个判定携带机器可读 reason 代码、人类可读说明与关键状态（版本、根、键指纹）。
失败分类是测试契约的一部分：测试会断言具体 reason，而非仅断言“调用成功/失败”。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Decision(str, Enum):
    ACCEPT = "ACCEPT"          # 证明核验通过
    REJECT = "REJECT"          # 证明核验失败（密码学或绑定关系不成立）
    INCONCLUSIVE = "INCONCLUSIVE"  # 材料不足，无法判定（未知根、格式损坏、签名链断裂等）


class Reason(str, Enum):
    # 接受
    MEMBERSHIP_VERIFIED = "MEMBERSHIP_VERIFIED"
    NON_MEMBERSHIP_VERIFIED = "NON_MEMBERSHIP_VERIFIED"
    REPLAY_VERIFIED = "REPLAY_VERIFIED"
    # —— 拒绝：密码学核验确实失败 ——
    ROOT_MISMATCH = "ROOT_MISMATCH"                      # 重算根 != 证据绑定根
    VALUE_MISMATCH = "VALUE_MISMATCH"                    # 叶值与声明值不符
    KEY_BINDING_MISMATCH = "KEY_BINDING_MISMATCH"        # 碰撞叶不属于被证键路径
    DEPTH_MISMATCH = "DEPTH_MISMATCH"                    # 深度/键宽参数不匹配
    KIND_MISMATCH = "KIND_MISMATCH"                      # 成员证明却为空、非成员却有叶等
    DEPTH_BOUND_REACHED = "DEPTH_BOUND_REACHED"          # 到树底仍未分流（参数化测试用碰撞夹具）
    SIGNATURE_INVALID = "SIGNATURE_INVALID"              # 检查点签名错误
    PARENT_ROOT_MISMATCH = "PARENT_ROOT_MISMATCH"        # 版本链 parent_root 断链
    # —— 无法判定：缺材料/材料格式问题 ——
    ENVELOPE_MALFORMED = "ENVELOPE_MALFORMED"
    UNKNOWN_ROOT = "UNKNOWN_ROOT"
    ENCODING_ERROR = "ENCODING_ERROR"
    INCONCLUSIVE = "INCONCLUSIVE"
    PUBLIC_KEY_UNAVAILABLE = "PUBLIC_KEY_UNAVAILABLE"


@dataclass
class Verdict:
    decision: Decision
    reason: Reason
    message: str
    key_fingerprint: str | None = None
    claimed_root: str | None = None
    recomputed_root: str | None = None
    version: int | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason": self.reason.value,
            "message": self.message,
            "key_fingerprint": self.key_fingerprint,
            "claimed_root": self.claimed_root,
            "recomputed_root": self.recomputed_root,
            "version": self.version,
            "detail": self.detail,
        }

    @property
    def accepted(self) -> bool:
        return self.decision is Decision.ACCEPT
