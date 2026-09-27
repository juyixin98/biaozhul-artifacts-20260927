"""Evaluate a canonical query tree against the SQLite index.

Every node is evaluated to the *set of matching doc ids* plus an
``explain`` record exposing the per-step inputs and the judgment basis
(matched terms, positions, bounds). Scores are a deterministic,
monotonic leaf-hit count (each matching positive leaf contributes 1;
NOT contributes nothing; a single leaf match scores 1), used only for
ordering: ties break by ``doc_id`` ascending.

Boolean semantics on a finite document universe ``U``::

    MatchAll -> U                     (the empty query matches everything)
    MatchNone -> {}
    AND -> intersection ; OR -> union ; NOT -> U \\ child
    nonexistent field leaves never reach execution (validate rejects them)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from searchdsl.analysis import parse_int, parse_date, tokenize
from searchdsl.astnodes import (
    And,
    MatchAll,
    MatchNone,
    Node,
    Not,
    Or,
    Phrase,
    Range,
    Term,
)
from searchdsl.spec import Schema
from searchdsl.store import Store


@dataclass
class EvalStep:
    op: str
    field_name: Optional[str]
    detail: dict
    matched: list[str]
    children: list["EvalStep"] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {
            "op": self.op,
            "field": self.field_name,
            "matched": self.matched,
            "detail": self.detail,
        }
        if self.children:
            d["children"] = [c.as_dict() for c in self.children]
        return d


@dataclass
class EvalResult:
    docs: set[str]
    scores: dict[str, int]
    steps: EvalStep

    def ordered(self) -> list[str]:
        return sorted(self.docs, key=lambda d: (-self.scores.get(d, 0), d))


class Executor:
    def __init__(self, store: Store, schema: Schema):
        self.store = store
        self.schema = schema
        self.universe = set(store.all_doc_ids())

    # -- leaf matching ---------------------------------------------------

    def _text_phrase_docs(self, field_name: str, tokens: list[str]) -> tuple[set[str], dict]:
        if not tokens:
            return set(), {"tokens": [], "reason": "empty phrase"}
        position_maps = [self.store.term_positions(field_name, t) for t in tokens]
        candidates = set(position_maps[0]) if position_maps else set()
        for m in position_maps[1:]:
            candidates &= set(m)
        matched: set[str] = set()
        for doc_id in candidates:
            starts = position_maps[0][doc_id]
            if any(
                all((s + k) in position_maps[k][doc_id] for k in range(1, len(tokens)))
                for s in starts
            ):
                matched.add(doc_id)
        return matched, {"tokens": tokens}

    def _text_term_docs(self, field_name: str, tokens: list[str]) -> tuple[set[str], dict]:
        if not tokens:
            return set(), {"tokens": [], "reason": "no analyzable tokens"}
        sets = [self.store.term_docs(field_name, t) for t in tokens]
        matched: set[str] = set(sets[0]) if sets else set()
        for s in sets[1:]:
            matched &= s
        return matched, {"tokens": tokens, "combine": "all tokens within one field"}

    def _eval_term_field(self, node: Term, field_name: str) -> tuple[set[str], dict]:
        spec = self.schema.get(field_name)
        if spec.type == "text":
            toks = tokenize(node.value)
            docs, detail = self._text_term_docs(field_name, toks)
            return docs, detail
        if spec.type == "keyword":
            docs = self.store.keyword_docs(field_name, node.value)
            return docs, {"exact_keyword": node.value}
        if spec.type == "int":
            num = parse_int(node.value)
            docs = self.store.scalar_docs(field_name, exact=str(num), numeric=True)
            return docs, {"exact_int": num}
        iso = parse_date(node.value)
        docs = self.store.scalar_docs(field_name, exact=iso, numeric=False)
        return docs, {"exact_date": iso}

    def _eval_phrase_field(self, node: Phrase, field_name: str) -> tuple[set[str], dict]:
        toks = tokenize(node.value)
        return self._text_phrase_docs(field_name, toks)

    # -- tree walk -------------------------------------------------------

    def eval(self, node: Node) -> tuple[set[str], dict[str, int], EvalStep]:
        """Return (matching docs, per-doc leaf-hit scores, explanation)."""
        if isinstance(node, MatchAll):
            docs = set(self.universe)
            return docs, {}, EvalStep(
                "match_all", None,
                {"reason": "empty query matches every document"},
                sorted(docs),
            )
        if isinstance(node, MatchNone):
            return set(), {}, EvalStep(
                "match_none", None,
                {"reason": "contradiction after simplification"}, [],
            )

        if isinstance(node, Term):
            return self._term_step(node)
        if isinstance(node, Phrase):
            return self._phrase_step(node)
        if isinstance(node, Range):
            return self._range_step(node)

        if isinstance(node, Not):
            child_docs, _, child_step = self.eval(node.child)
            docs = self.universe - child_docs
            return docs, {}, EvalStep(
                "not", None,
                {"universe_size": len(self.universe), "excluded": sorted(child_docs)},
                sorted(docs), [child_step],
            )

        if isinstance(node, (And, Or)):
            parts = [self.eval(c) for c in node.children]
            if isinstance(node, And):
                docs = set(self.universe)
                for d, _, _ in parts:
                    docs &= d
                combine = "intersection"
            else:
                docs = set()
                for d, _, _ in parts:
                    docs |= d
                combine = "union"
            scores: dict[str, int] = {}
            for d in docs:
                scores[d] = sum(child_scores.get(d, 0) for _, child_scores, _ in parts)
            step = EvalStep(
                "and" if isinstance(node, And) else "or",
                None,
                {"combine": combine, "inputs": len(parts)},
                sorted(docs),
                [p[2] for p in parts],
            )
            return docs, scores, step

        raise RuntimeError(f"cannot execute node: {node!r}")

    def _term_step(self, node: Term) -> tuple[set[str], dict[str, int], EvalStep]:
        if node.field_name:
            docs, detail = self._eval_term_field(node, node.field_name)
            scores = {d: 1 for d in docs}
            return docs, scores, EvalStep("term", node.field_name, detail, sorted(docs))
        docs: set[str] = set()
        per_field = []
        for f in self.schema.default_fields:
            fdocs, fdetail = self._eval_term_field(node, f)
            per_field.append({"field": f, "matched": sorted(fdocs), **fdetail})
            docs |= fdocs
        scores = {d: 1 for d in docs}
        return docs, scores, EvalStep(
            "term", None,
            {"default_fields": list(self.schema.default_fields), "per_field": per_field},
            sorted(docs),
        )

    def _phrase_step(self, node: Phrase) -> tuple[set[str], dict[str, int], EvalStep]:
        if node.field_name:
            docs, detail = self._eval_phrase_field(node, node.field_name)
            scores = {d: 1 for d in docs}
            return docs, scores, EvalStep("phrase", node.field_name, detail, sorted(docs))
        docs: set[str] = set()
        per_field = []
        for f in self.schema.default_fields:
            fdocs, fdetail = self._eval_phrase_field(node, f)
            per_field.append({"field": f, "matched": sorted(fdocs), **fdetail})
            docs |= fdocs
        scores = {d: 1 for d in docs}
        return docs, scores, EvalStep(
            "phrase", None,
            {"default_fields": list(self.schema.default_fields), "per_field": per_field},
            sorted(docs),
        )

    def _range_step(self, node: Range) -> tuple[set[str], dict[str, int], EvalStep]:
        spec = self.schema.get(node.field_name)
        numeric = spec.type == "int"
        docs = self.store.scalar_docs(
            node.field_name,
            gte=node.gte,
            gt=node.gt,
            lte=node.lte,
            lt=node.lt,
            numeric=numeric,
        )
        detail = {
            "bounds": {"gte": node.gte, "gt": node.gt, "lte": node.lte, "lt": node.lt},
            "value_type": spec.type,
        }
        scores = {d: 1 for d in docs}
        return docs, scores, EvalStep("range", node.field_name, detail, sorted(docs))


def execute(node: Node, store: Store, schema: Schema) -> EvalResult:
    executor = Executor(store, schema)
    docs, scores, steps = executor.eval(node)
    return EvalResult(docs=docs, scores=scores, steps=steps)
