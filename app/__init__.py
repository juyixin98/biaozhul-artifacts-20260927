"""日志脱敏服务（log-redaction-service）。

模块划分：
- rules/   规则/证据解析（规则校验、规则档加载）
- core/    安全内核（流式 JSON 词法扫描、跨块脱敏内核）
- state/   状态隔离（会话状态 + SQLite 加密审计存储）
- audit/   审计接口
- api/     FastAPI 路由与模式层

仅使用本地合成夹具，不接入任何真实业务数据。
"""

__version__ = "1.0.0"
