"""Unicode 词条压缩 Trie 补全后端。

分层组织：
- normalizer.py  文本规范化（版本固定）
- trie.py        压缩 Trie（Patricia trie）索引与精确 top-k 分支限界
- oracle.py      独立暴力参照实现（测试用，不依赖 trie 内部）
- store.py       SQLite 版本存储与持久快照
- engine.py      索引 + 存储的编排
- main.py        FastAPI 查询/诊断接口
"""
from __future__ import annotations

from .normalizer import NORMALIZER_VERSION, normalize

SCHEMA_VERSION = "schema-1"
SERVICE_NAME = "unicode-compressed-trie"

__all__ = ["NORMALIZER_VERSION", "SCHEMA_VERSION", "SERVICE_NAME", "normalize"]
