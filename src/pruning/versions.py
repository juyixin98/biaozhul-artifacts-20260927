"""版本固定。

时区与日期转换规则一旦发布即冻结，所有计划结果与日志都会回带这些版本号，
保证同一份谓词在任何时间得到完全一致的裁剪结论（可复现 / 可审计）。
"""

# 日期变换版本：UTC 固定时区、civil-date 月份桶算法（见 transforms.py）
DATE_TRANSFORM_VERSION = "dateconv-1.0.0"
# 文件统计序列化版本（min/max/null_count、字符串截断标志的含义）
STATS_FORMAT_VERSION = "stats-1.0.0"
# SQLite 元数据 schema 版本（PRAGMA user_version）
METADATA_SCHEMA_VERSION = "meta-1.0.0"
METADATA_USER_VERSION = 1
# 执行内核版本（裁剪推理规则）
KERNEL_VERSION = "kernel-1.0.0"

# 固定时区：所有时间戳与日期字面量一律按 UTC 解释，禁止读取系统本地时区
FIXED_TIMEZONE = "UTC"


def version_bundle() -> dict:
    """返回需要在接口响应与日志中展示的版本集合。"""
    return {
        "date_transform": DATE_TRANSFORM_VERSION,
        "stats_format": STATS_FORMAT_VERSION,
        "metadata_schema": METADATA_SCHEMA_VERSION,
        "kernel": KERNEL_VERSION,
        "timezone": FIXED_TIMEZONE,
    }
