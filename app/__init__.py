"""本地响应元数据缓存键与 Vary 配置审计后端。

模块分层：
- parser:  规则/证据解析（头部归一化、Vary、Cache-Control、报文摘要）
- kernel:  安全内核（缓存键派生、身份隔离、碰撞见证）
- crypto:  密文封装与事件链 HMAC
- storage: SQLite 状态隔离与审计日志
- api:     FastAPI 审计接口
"""

__version__ = "1.0.0"
