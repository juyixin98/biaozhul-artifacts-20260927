"""离线对象存储策略差分分析系统。

模块职责（彼此不反向依赖核心实现）：
- parser:   规则/证据解析与严格校验
- kernel:   安全内核（三态判定、显式拒绝优先、默认拒绝）
- universe: 受限请求空间的区域划分与穷举
- evidence: 历史证据解析与核验
- crypto_verify: Ed25519 签名与规范 JSON
- store:    SQLite 状态隔离（不可变快照）
- audit:    审计日志（关联请求身份/版本/处理位置）
- api:      FastAPI 审计接口
"""

__version__ = "1.0.0"
