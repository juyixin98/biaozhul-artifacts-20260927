"""可撤回链上派生索引（reorg-aware derived index）。

模块职责划分见 README：
- crypto     编码/哈希/验签（纯函数，无链状态）
- models     数据模型与序列化
- storage    SQLite 持久化（区块、悬挂池、派生表、诊断事件）
- consensus  固定的分叉权重/最终性规则
- kernel     链状态内核：连接、挂起、切换、撤回、重放
- replay     离线夹具回放与全量重建核对
- api        FastAPI 只读/写入接口
"""

__version__ = "0.1.0"
