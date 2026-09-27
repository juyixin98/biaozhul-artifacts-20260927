"""独立参考求值器：纯 Python 直接在内存文档集合上判真值。

刻意不使用倒排表 / SQL，也不与 index.py 共享分词代码，
作为测试中的第二套独立答案来源（另一份是手工算出的固定期望集）。
"""

from __future__ import annotations

import re
from typing import Dict, List, Sequence

from . import ast_nodes as ast
from .schema import Schema

# 与 index.tokenize 口径一致，但实现独立（手写扫描而非 re.findall）。
def _tokens(text: str) -> List[str]:
    out: List[str] = []
    buf: List[str] = []
    for ch in str(text).lower():
        if ch.isalnum() and ch.isascii():
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out


def _phrase_hit(sequence: List[str], wanted: List[str]) -> bool:
    if not wanted or len(wanted) > len(sequence):
        return False
    last = len(sequence) - len(wanted)
    for i in range(last + 1):
        if sequence[i:i + len(wanted)] == wanted:
            return True
    return False


class ReferenceEvaluator:
    """对 [{id, title, body, author, tags, year}, ...] 直接求值。"""

    def __init__(self, documents: Sequence[dict], schema: Schema) -> None:
        self.docs: Dict[str, dict] = {d["id"]: d for d in documents}
        self.schema = schema

    def evaluate(self, node: ast.Node) -> set[str]:
        if isinstance(node, ast.Empty):
            return set(self.docs)
        if isinstance(node, ast.Term):
            return self._term(node)
        if isinstance(node, ast.Phrase):
            return self._phrase(node)
        if isinstance(node, ast.Not):
            return set(self.docs) - self.evaluate(node.child)
        if isinstance(node, ast.And):
            result = set(self.docs)
            for child in node.children:
                result &= self.evaluate(child)
            return result
        if isinstance(node, ast.Or):
            result: set[str] = set()
            for child in node.children:
                result |= self.evaluate(child)
            return result
        raise TypeError(f"unknown node: {node!r}")

    def _candidate_fields(self, field) -> List[str]:
        return [field] if field else self.schema.default_fields

    def _term(self, node: ast.Term) -> set[str]:
        result: set[str] = set()
        for doc_id, doc in self.docs.items():
            for field in self._candidate_fields(node.field):
                raw = doc.get(field)
                if raw is None:
                    continue
                if self.schema.field_types[field] == "int":
                    if str(int(raw)) == str(int(node.value)):
                        result.add(doc_id)
                elif node.value.lower() in _tokens(raw):
                    result.add(doc_id)
        return result

    def _phrase(self, node: ast.Phrase) -> set[str]:
        wanted = [t.lower() for t in node.terms]
        result: set[str] = set()
        for doc_id, doc in self.docs.items():
            for field in self._candidate_fields(node.field):
                if self.schema.field_types[field] != "text":
                    continue
                if _phrase_hit(_tokens(doc.get(field, "")), wanted):
                    result.add(doc_id)
        return result
