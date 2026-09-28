"""local-txpool：本地账户交易池后端。

模块分工（核心机制不在任何单一文件中硬编码演示）：

- ``localtxpool.encoding``：RLP 编码/解码、EIP-155 签名与恢复（自带编解码实现）。
- ``localtxpool.core``：链状态内核（可执行前缀、替换、过期、容量、候选区块、确认/回滚）。
- ``localtxpool.storage``：SQLite 索引存储（唯一约束保证同 nonce 不并存有效交易）。
- ``localtxpool.replay``：离线回放（确定性虚拟时钟 + 事件文件 + 期望核对）。
- ``localtxpool.api``：FastAPI HTTP 接口。
"""

__version__ = "1.0.0"
