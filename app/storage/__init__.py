"""索引存储层：SQLite 持久化。

表职责：
* ``nodes``       —— 内容寻址节点（叶/分支），append-only，旧根的历史节点不删。
* ``versions``    —— 版本链：version、root、parent_root、batch_id、签名、时间戳。
* ``key_index``   —— 当前存活键 -> 所在版本（成员判断、按键取证、回放交叉核验）。
* ``journal``     —— 规范化批次的逐操作流水（离线回放按它独立重建）。
* ``idempotency`` —— 批次幂等键：同键同载荷返回同版本，同键异载荷冲突。
"""
