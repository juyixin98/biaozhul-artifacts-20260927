"""跨模块统一的错误契约。

所有领域错误都带稳定的 ``category``（失败类别）与 ``code``（具体原因），
HTTP 状态码由类别映射。测试按具体 code 断言，而不是只断言“接口报错”。

四类可区分失败（对应验收要求中的输入错误 / 状态冲突 / 资源耗尽 / 计算失败）：
  input_error      422  证据或策略本身不合法
  state_conflict   409  运行状态不允许该操作（如已提交后改策略、二次封口）
  resource_exhausted 507 超出每运行资源上限（证据数 / 单证据体积）
  computation_failure 422 校验/加密等计算无法完成（摘要不符、密文损坏等）
"""
from __future__ import annotations

from typing import Any


class ErrorCode:
    # ---- input_error ----
    POLICY_INVALID = "policy_invalid"                # 策略字段非法或自相矛盾
    EVIDENCE_INVALID = "evidence_invalid"            # 证据字段缺失/格式错误
    RUN_ID_INVALID = "run_id_invalid"                # run_id 字符集/长度非法
    PAGINATION_INVALID = "pagination_invalid"        # limit/offset 非法
    # ---- state_conflict ----
    RUN_EXISTS = "run_exists"                        # run_id 已被占用
    RUN_NOT_FOUND = "run_not_found"                  # 运行不存在（不泄漏他人内容）
    POLICY_NOT_SET = "policy_not_set"                # 未配置策略就提交证据/分析
    POLICY_LOCKED = "policy_locked"                  # 已有证据后策略不可变
    RUN_NOT_OPEN = "run_not_open"                   # 封口/终结的运行拒绝写入
    NOTHING_TO_ANALYZE = "nothing_to_analyze"        # 零证据不允许分析
    # ---- resource_exhausted ----
    TOO_MANY_EVIDENCE = "too_many_evidence"          # 每运行证据条数超限
    EVIDENCE_TOO_LARGE = "evidence_too_large"        # 单证据 body 超限
    # ---- computation_failure ----
    BODY_HASH_MISMATCH = "body_hash_mismatch"        # 声明的 body_sha256 与实算不符
    CHAIN_VERIFICATION_FAILED = "chain_verification_failed"  # 事件链被篡改
    CIPHERTEXT_INVALID = "ciphertext_invalid"        # 密文无法解开
    KEY_DERIVATION_FAILED = "key_derivation_failed"  # 主密钥格式非法

    CATEGORY_STATUS: dict[str, int] = {
        "input_error": 422,
        "state_conflict": 409,
        "resource_exhausted": 507,
        "computation_failure": 422,
    }

    # 个别 code 的 HTTP 语义与类别默认状态不同（类别仍用于机器判定）
    STATUS_OVERRIDE: dict[str, int] = {
        RUN_NOT_FOUND: 404,
    }


class AuditError(Exception):
    """所有领域错误的基类。"""

    category: str = "input_error"
    code: str = "evidence_invalid"

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def status_code(self) -> int:
        return ErrorCode.STATUS_OVERRIDE.get(
            self.code, ErrorCode.CATEGORY_STATUS[self.category]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "category": self.category,
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class InputError(AuditError):
    category = "input_error"

    def __init__(self, message: str, code: str = ErrorCode.EVIDENCE_INVALID,
                 details: dict[str, Any] | None = None):
        super().__init__(code, message, details)


class StateConflictError(AuditError):
    category = "state_conflict"

    def __init__(self, message: str, code: str = ErrorCode.RUN_NOT_OPEN,
                 details: dict[str, Any] | None = None):
        super().__init__(code, message, details)


class ResourceExhaustedError(AuditError):
    category = "resource_exhausted"

    def __init__(self, message: str, code: str = ErrorCode.TOO_MANY_EVIDENCE,
                 details: dict[str, Any] | None = None):
        super().__init__(code, message, details)


class ComputationFailureError(AuditError):
    category = "computation_failure"

    def __init__(self, message: str, code: str = ErrorCode.BODY_HASH_MISMATCH,
                 details: dict[str, Any] | None = None):
        super().__init__(code, message, details)
