"""领域错误与错误语义。

服务层只抛这些异常，API 层统一翻译成 HTTP 响应。**未知异常不伪装成功**：
未预期错误返回 500 并带错误 ID（写日志），响应里不暴露内部堆栈。
"""

from __future__ import annotations


class DomainError(Exception):
    """所有可预期领域错误的基类。"""

    error_code = "DOMAIN_ERROR"
    http_status = 400


class ValidationFailure(DomainError):
    """输入校验失败（空词条、非法分值、非法 k 等）。"""

    error_code = "VALIDATION_ERROR"
    http_status = 400

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class EntryNotFound(DomainError):
    """删除/查询的词条不存在。"""

    error_code = "ENTRY_NOT_FOUND"
    http_status = 404


class VersionNotFound(DomainError):
    """版本或快照不存在；或对未物化快照的版本做历史查询。"""

    error_code = "VERSION_NOT_FOUND"
    http_status = 404


class NormalizerMismatch(DomainError):
    """数据以未知/不兼容的规范化版本写入，拒绝用当前管线解释。"""

    error_code = "NORMALIZER_MISMATCH"
    http_status = 409


class IndexDegraded(DomainError):
    """存储已提交但内存索引更新失败 —— 明确报错，绝不返回过期结果。"""

    error_code = "INDEX_DEGRADED"
    http_status = 500


class SnapshotConflict(DomainError):
    """快照相关冲突（例如重复恢复等）。"""

    error_code = "SNAPSHOT_CONFLICT"
    http_status = 409
