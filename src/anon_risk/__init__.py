"""anon-risk：匿名化等价类风险检查后端。

分层（自下而上，均为可独立测试的真实实现）：

- ``anon_risk.kernel``  纯函数安全内核：解析/校验、泛化层级、等价类、
  k-匿名 / l-多样性指标、穷举最优泛化建议。不依赖 Web、数据库。
- ``anon_risk.security`` 密钥派生、加密、脱敏键（HMAC）、出站白名单视图。
- ``anon_risk.storage`` 每次运行独立的加密 SQLite 状态；只追加的审计库。
- ``anon_risk.service`` 编排层：运行隔离、审计、运行身份关联。
- ``anon_risk.api`` FastAPI 路由与错误映射。
"""

__version__ = "1.0.0"
