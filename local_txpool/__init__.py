"""local-txpool: 本地合成账户交易池后端服务。

模块分层::

    core/     编码与验签 (crypto)、链状态内核 (kernel)、候选排序 (ordering)、配置
    storage/  SQLite 索引存储 (repository)
    offline/  夹具场景执行与独立回放
    api/      FastAPI HTTP 层
"""

__version__ = "1.0.0"
