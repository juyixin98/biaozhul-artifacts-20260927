"""失败类别定义。

所有可预期的解析/对比失败都有稳定的 category 字符串,
测试与诊断按类别断言,而不是匹配自由文本。
"""

from __future__ import annotations

from enum import Enum


class FailureCategory(str, Enum):
    # 解析期
    MISSING_EXTM3U = "missing-extm3u"
    DUPLICATE_TAG = "duplicate-tag"
    CONTENT_AFTER_ENDLIST = "content-after-endlist"
    MISSING_EXTINF = "missing-extinf"
    DANGLING_EXTINF = "dangling-extinf"
    BAD_TAG_VALUE = "bad-tag-value"
    ENCRYPTION_UNSUPPORTED = "encryption-unsupported"
    BYTE_RANGE_OFFSET_UNRESOLVABLE = "byte-range-offset-unresolvable"
    # 对比期
    APPEND_AFTER_ENDLIST = "append-after-endlist"
    CONTENT_RETRACTED = "content-retracted"
    SEGMENT_CONFLICT = "segment-conflict"
    # 服务期
    STREAM_NOT_FOUND = "stream-not-found"
    JOB_NOT_FOUND = "job-not-found"
    VERSION_NOT_FOUND = "version-not-found"


class PlaylistParseError(Exception):
    """解析失败。category 供调用方分类处理。"""

    def __init__(self, category: FailureCategory, detail: str, line_no: int | None = None):
        self.category = category
        self.detail = detail
        self.line_no = line_no
        where = f" (line {line_no})" if line_no is not None else ""
        super().__init__(f"{category.value}: {detail}{where}")
