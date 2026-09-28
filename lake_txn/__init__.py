"""lake_txn —— 简化湖表元数据事务服务。

模块职责：
- config:          配置加载与路径约定
- errors:          领域错误码与异常
- diagnostics:     结构化诊断日志、脱敏、关联标识
- format_adapter:  Parquet 格式适配（PyArrow）
- kernel:          纯逻辑执行内核（冲突裁决、清单规划），无 I/O
- metadata:        SQLite 元数据事务（快照、清单、暂存台账、清理台账）
- service:         编排层：暂存发布、两阶段提交、孤立文件清扫
- api/app:         FastAPI 验证接口
"""

__version__ = "0.1.0"
