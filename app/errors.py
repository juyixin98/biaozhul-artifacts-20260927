"""错误契约。

所有可预期的失败都抛出 :class:`ServiceError` 的子类，携带稳定的机器可读
``code`` 与 HTTP 状态码。类别划分（对应需求“输入错误、状态冲突、资源耗尽、
计算失败须可区分”）：

* 输入错误 (4xx, ``INPUT_*``)           —— 调用方可修复的请求问题
* 状态冲突 (409, ``STATE_*``)           —— 版本不符、状态机非法转移
* 资源耗尽 (413/422, ``RESOURCE_*``)    —— 文本/程序/条目数量超过预算
* 计算失败 (422/500, ``COMPUTE_*``)     —— 引擎/模板编译期或求值期失败

``details`` 内字段保持稳定，测试按字段断言“失败类别”，而非仅查状态码。
"""
from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """业务错误基类。"""

    category: str = "service_error"
    code: str = "SERVICE_ERROR"
    http_status: int = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "category": self.category,
                "message": self.message,
                "details": self.details,
            }
        }


# --------------------------------------------------------------------------- #
# 输入错误
# --------------------------------------------------------------------------- #
class InputError(ServiceError):
    category = "input"
    http_status = 422


class TextNotUnicodeError(InputError):
    """文本必须是合法 Unicode（本服务以 Python str / UTF-8 为规范）。"""

    code = "INPUT_TEXT_NOT_UNICODE"


class PayloadTooLargeError(InputError):
    """文本长度超限。状态语义取 413。"""

    code = "INPUT_TEXT_TOO_LARGE"
    http_status = 413


class InvalidRuleError(InputError):
    """规则字段非法（id 重复/过长、pattern/template 越界等）。"""

    code = "INPUT_INVALID_RULE"


class InvalidTemplateError(InputError):
    """捕获引用在“计划前验证”阶段不合法（引用不存在的组、组重复定义等）。"""

    code = "INPUT_INVALID_TEMPLATE"


class InvalidFlagError(InputError):
    code = "INPUT_INVALID_FLAG"


# --------------------------------------------------------------------------- #
# 计算失败
# --------------------------------------------------------------------------- #
class ComputeError(ServiceError):
    category = "compute"
    http_status = 422


class RegexCompileError(ComputeError):
    """正则无法被无回溯引擎接受（回溯引用/环视等）或语法错误。"""

    code = "COMPUTE_REGEX_COMPILE"


class RegexProgramTooLargeError(ComputeError):
    """RE2 程序超出 max_mem 预算——编译期资源耗尽，归入计算失败但可区分。"""

    code = "COMPUTE_REGEX_PROGRAM_TOO_LARGE"
    http_status = 413


class CaptureUnavailableError(ComputeError):
    """模板引用的捕获组在本次匹配中未参与（可选组缺失）。

    仅当规则把该引用标记为严格（strict_captures，默认）时抛出；否则渲染为空串。
    """

    code = "COMPUTE_CAPTURE_UNAVAILABLE"


# --------------------------------------------------------------------------- #
# 资源耗尽（运行期）
# --------------------------------------------------------------------------- #
class ResourceExhaustedError(ServiceError):
    category = "resource"
    code = "RESOURCE_EXHAUSTED"
    http_status = 413


# --------------------------------------------------------------------------- #
# 状态冲突
# --------------------------------------------------------------------------- #
class StateConflictError(ServiceError):
    category = "state"
    code = "STATE_CONFLICT"
    http_status = 409


class SourceNotFoundError(StateConflictError):
    code = "STATE_SOURCE_NOT_FOUND"
    http_status = 404


class PlanNotFoundError(StateConflictError):
    code = "STATE_PLAN_NOT_FOUND"
    http_status = 404


class SourceVersionMismatchError(StateConflictError):
    """计划绑定的源摘要与当前源版本不一致——拒绝应用。"""

    code = "STATE_SOURCE_VERSION_MISMATCH"
    http_status = 409


class PlanAlreadyAppliedError(StateConflictError):
    code = "STATE_PLAN_ALREADY_APPLIED"
    http_status = 409
