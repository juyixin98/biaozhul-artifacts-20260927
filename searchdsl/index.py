"""基于 SQLite 倒排表（带位置）的查询执行。

索引端词元化与参考求值器（evaluator.py）是两份独立实现；
二者只共享 docs/spec.md 规定的匹配口径：
- 词元 = 正则 [a-z0-9]+（索引时小写化，查询值同样小写化）；
- 大小写不敏感；int 字段按整数字面量等值匹配；
- 短语要求词序列在同一字段内位置连续。
"""

from __future__ import annotations

import json
import re
from typing import Dict, Iterable, List

from . import ast_nodes as ast
from .schema import Schema
from .store import Store

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: object) -> List[str]:
    return _TOKEN_RE.findall(str(text).lower())


class Index:
    def __init__(self, store: Store, schema: Schema) -> None:
        self.store = store
        self.schema = schema

    def rebuild(self, documents: Iterable[dict]) -> int:
        docs = list(documents)
        self.store.reset_documents(docs)
        rows = []
        for doc in docs:
            for field in self.schema.fields:
                raw = doc.get(field)
                if raw is None:
                    continue
                if self.schema.field_types[field] == "int":
                    rows.append((field, str(int(raw)), doc["id"], "[]"))
                else:
                    positions: Dict[str, List[int]] = {}
                    for pos, tok in enumerate(tokenize(raw)):
                        positions.setdefault(tok, []).append(pos)
                    for tok, poses in positions.items():
                        rows.append((field, tok, doc["id"], json.dumps(poses)))
        self.store.replace_postings(rows)
        return len(rows)

    def execute(self, node: ast.Node) -> set[str]:
        if isinstance(node, ast.Empty):
            return set(self.store.all_doc_ids())
        if isinstance(node, ast.Term):
            return self._term(node)
        if isinstance(node, ast.Phrase):
            return self._phrase(node)
        if isinstance(node, ast.Not):
            return set(self.store.all_doc_ids()) - self.execute(node.child)
        if isinstance(node, ast.And):
            result: set[str] | None = None
            for child in node.children:
                part = self.execute(child)
                result = part if result is None else result & part
            return result if result is not None else set()
        if isinstance(node, ast.Or):
            result = set()
            for child in node.children:
                result |= self.execute(child)
            return result
        raise TypeError(f"unknown node: {node!r}")

    def _fields_for(self, node) -> List[str]:
        return [node.field] if node.field else self.schema.default_fields

    def _term(self, node: ast.Term) -> set[str]:
        result: set[str] = set()
        for field in self._fields_for(node):
            if self.schema.field_types[field] == "int":
                term = str(int(node.value))  # 校验阶段已保证是整数
            else:
                term = node.value.lower()
            result.update(self.store.posting_docs(field, term))
        return result

    def _phrase(self, node: ast.Phrase) -> set[str]:
        wanted = [t.lower() for t in node.terms]
        result: set[str] = set()
        for field in self._fields_for(node):
            if self.schema.field_types[field] != "text":
                continue  # 校验阶段已拦截；防御性跳过
            first = self.store.posting_positions(field, wanted[0])
            for doc_id, poses in first.items():
                chains = [self.store.posting_positions(field, t).get(doc_id, [])
                          for t in wanted[1:]]
                for start in poses:
                    if all((start + offset + 1) in chain
                           for offset, chain in enumerate(chains)):
                        result.add(doc_id)
                        break
        return result
