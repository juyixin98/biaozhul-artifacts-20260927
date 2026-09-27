"""Independent reference oracle for truth-value testing.

This module deliberately does NOT import searchdsl.lexer / parser /
analysis / executor / normalize. It re-implements, in the simplest
possible way straight from the raw corpus dictionaries:

  * text analysis (Unicode normalization + regex tokenization + CJK
    unigrams) — written independently from the production analyzer;
  * per-document boolean evaluation of a query tree.

The oracle consumes the *same parsed/normalized node objects* the
production pipeline produces (using isinstance on the astnode classes
imported lazily in ``evaluate`` to keep this module's independence
explicit and auditable), but decides truth itself from raw field values.
It is intentionally O(N x tree) brute force with no index.

Conventions mirroring the product spec:
  text fields: NFKC + casefold tokens; a term matches iff ALL its tokens
    occur in the field; a phrase iff the token sequence appears
    consecutively.
  keyword: exact, case-sensitive string equality; multi-valued OR.
  int: exact term or inclusive/exclusive range on parsed ints.
  date: exact term or range on normalized ISO dates.
  unfielded term/phrase: OR over the default text fields.
  universe: every document id in the corpus.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date


def _cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0xF900 <= cp <= 0xFAFF
    )


_RUN = re.compile(r"\d+|[^\W\d_]+", re.UNICODE)


def ref_tokens(text: str) -> list[str]:
    """Independent text analysis (do not call searchdsl.analysis here)."""
    norm = unicodedata.normalize("NFKC", str(text)).casefold()
    out: list[str] = []
    for m in _RUN.finditer(norm):
        piece = m.group(0)
        if piece[0].isdigit():
            out.append(piece)
            continue
        buf = []
        for ch in piece:
            if _cjk(ch):
                if buf:
                    out.append("".join(buf))
                    buf = []
                out.append(ch)
            else:
                buf.append(ch)
        if buf:
            out.append("".join(buf))
    return out


def ref_int(raw: str) -> int:
    s = str(raw).strip()
    assert re.fullmatch(r"[+-]?\d+", s), f"bad int {raw!r}"
    return int(s)


def ref_date(raw: str) -> str:
    s = str(raw).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
    assert m, f"bad date {raw!r}"
    y, mo, d = (int(g) for g in m.groups())
    date(y, mo, d)  # raises on impossible calendar dates
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _field_values(doc: dict, field: str):
    fields = doc.get("fields", {})
    if field not in fields or fields[field] is None:
        return []
    val = fields[field]
    return val if isinstance(val, list) else [val]


def _text_contains(tokens_field: list[str], wanted: list[str]) -> bool:
    return all(w in tokens_field for w in wanted)


def _text_phrase(tokens_field: list[str], wanted: list[str]) -> bool:
    if not wanted:
        return False
    for i in range(0, len(tokens_field) - len(wanted) + 1):
        if tokens_field[i:i + len(wanted)] == wanted:
            return True
    return False


class Oracle:
    def __init__(self, schema: dict, docs: list[dict], default_fields):
        # schema: dict name -> {"type": ..., "multi_valued": bool}
        self.schema = schema
        self.docs = {str(d["doc_id"]): d for d in docs}
        self.defaults = tuple(default_fields)

    def set_defaults(self, default_fields):
        self.defaults = tuple(default_fields)

    # -- leaf truth ------------------------------------------------------

    def _term(self, node, doc) -> bool:
        field = node.field_name
        wanted = ref_tokens(node.value)
        if not field:
            return any(
                _text_contains(ref_tokens(v), wanted)
                for f in self.defaults
                for v in _field_values(doc, f)
            )
        sp = self.schema[field]
        vals = _field_values(doc, field)
        if sp["type"] == "text":
            return any(_text_contains(ref_tokens(v), wanted) for v in vals)
        if sp["type"] == "keyword":
            return any(str(v) == node.value for v in vals)
        if sp["type"] == "int":
            try:
                target = ref_int(node.value)
            except (AssertionError, ValueError):
                return False
            return any(ref_int(v) == target for v in vals)
        if sp["type"] == "date":
            try:
                target = ref_date(node.value)
            except (AssertionError, ValueError):
                return False
            return any(ref_date(v) == target for v in vals)
        raise AssertionError(f"unknown type {sp['type']}")

    def _phrase(self, node, doc) -> bool:
        wanted = ref_tokens(node.value)
        if not wanted:
            return False
        fields = (node.field_name,) if node.field_name else self.defaults
        for f in fields:
            for v in _field_values(doc, f):
                if _text_phrase(ref_tokens(v), wanted):
                    return True
        return False

    def _range(self, node, doc) -> bool:
        sp = self.schema[node.field_name]
        vals = _field_values(doc, node.field_name)
        for v in vals:
            if sp["type"] == "int":
                x = ref_int(v)
                low = ref_int(node.gte) if node.gte is not None else None
                lowx = ref_int(node.gt) if node.gt is not None else None
                high = ref_int(node.lte) if node.lte is not None else None
                highx = ref_int(node.lt) if node.lt is not None else None
            else:
                x = ref_date(v)
                low = ref_date(node.gte) if node.gte is not None else None
                lowx = ref_date(node.gt) if node.gt is not None else None
                high = ref_date(node.lte) if node.lte is not None else None
                highx = ref_date(node.lt) if node.lt is not None else None
            if low is not None and not x >= low:
                continue
            if lowx is not None and not x > lowx:
                continue
            if high is not None and not x <= high:
                continue
            if highx is not None and not x < highx:
                continue
            return True
        return False

    # -- boolean evaluation ---------------------------------------------

    def eval_doc(self, node, doc) -> bool:
        from searchdsl.astnodes import (
            And,
            MatchAll,
            MatchNone,
            Not,
            Or,
            Phrase,
            Range,
            Term,
        )

        if isinstance(node, MatchAll):
            return True
        if isinstance(node, MatchNone):
            return False
        if isinstance(node, Term):
            return self._term(node, doc)
        if isinstance(node, Phrase):
            return self._phrase(node, doc)
        if isinstance(node, Range):
            return self._range(node, doc)
        if isinstance(node, Not):
            return not self.eval_doc(node.child, doc)
        if isinstance(node, And):
            return all(self.eval_doc(c, doc) for c in node.children)
        if isinstance(node, Or):
            return any(self.eval_doc(c, doc) for c in node.children)
        raise AssertionError(f"oracle cannot evaluate {node!r}")

    def evaluate(self, node) -> set[str]:
        return {
            doc_id
            for doc_id, doc in sorted(self.docs.items())
            if self.eval_doc(node, doc)
        }
