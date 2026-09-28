"""统一错误契约。

四类可区分失败 + 资源不存在：

| 异常               | HTTP | code                  | 语义类别   |
|--------------------|------|-----------------------|------------|
| InputInvalid       | 400  | INPUT_INVALID         | 输入错误   |
| StateConflict      | 409  | STATE_CONFLICT/*reason| 状态冲突   |
| ResourceExhausted  | 413  | RESOURCE_EXHAUSTED/*   | 资源耗尽   |
| ComputationFailed  | 500  | COMPUTATION_FAILED/*   | 计算失败   |
| DocNotFound        | 404  | DOC_NOT_FOUND          | 资源不存在 |

所有错误体形如：
{"error": {"code", "reason", "message", "details", "request_id"}}
"""


class OTError(Exception):
    status: int = 400
    code: str = "INPUT_INVALID"

    def __init__(self, message: str, *, reason: str | None = None, details: dict | None = None):
        super().__init__(message)
        self.message = message
        # reason 是更细的失败子类，便于测试断言“失败类别”而非仅状态码
        self.reason = reason or self.code
        self.details = details or {}

    def to_body(self, request_id: str | None = None) -> dict:
        return {
            "error": {
                "code": self.code,
                "reason": self.reason,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class InputInvalid(OTError):
    status = 400
    code = "INPUT_INVALID"


class StateConflict(OTError):
    status = 409
    code = "STATE_CONFLICT"


class ResourceExhausted(OTError):
    status = 413
    code = "RESOURCE_EXHAUSTED"


class ComputationFailed(OTError):
    status = 500
    code = "COMPUTATION_FAILED"


class DocNotFound(OTError):
    status = 404
    code = "DOC_NOT_FOUND"
