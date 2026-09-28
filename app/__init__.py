"""湖表读时删除应用器（Read-Time Delete Applier, RTDA）。

模块边界：
- contracts/  数据与错误契约（schema、请求模型），不依赖 pyarrow
- adapters/   格式适配层（Parquet 读写、内容哈希、类型映射）
- kernel/     执行内核（过滤 DSL、读时应用扫描器、独立参考 oracle）
- metadata/   元数据事务（SQLite：表、快照、清单、删除文件、事件、运行日志）
- services/   事务性服务（提交规划、提交执行、校验、运行记录）
- api/        FastAPI 验证接口
"""

__version__ = "1.0.0"
