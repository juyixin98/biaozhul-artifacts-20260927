"""Unicode 压缩 Trie 补全后端。

分层组织：

- ``app.normalize``  文本规范化（版本固定的规范化管线）
- ``app.trie``       算法索引（压缩 Radix Trie + 可靠上界 + 精确 top-k）
- ``app.storage``    版本存储（SQLite，事件/版本/快照）
- ``app.engine``     领域服务（把存储与索引绑定，提供不变量诊断）
- ``app.api``        HTTP 查询与诊断接口（FastAPI）
- ``app.config``     配置层
"""

__version__ = "1.0.0"
