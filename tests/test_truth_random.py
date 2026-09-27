"""Random small-expression differential tests.

For each seed a small random boolean expression is built *over a fixed
vocabulary*, rendered to DSL text, and evaluated three ways:

  1. the independent :mod:`tests.oracle` on the raw parsed tree,
  2. the production executor on the normalized tree,
  3. the oracle on the normalized tree (a second, index-free check).

Truth sets must agree for (1)/(2)/(3), the normalized tree must parse
again from its re-rendered form to the same canonical hash, and a second
normalization must be identical (idempotence). The expected answers are
NOT produced by the core implementation — the oracle is a standalone
brute-force evaluator over the raw corpus dictionaries.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from searchdsl.astnodes import (
    And,
    MatchAll,
    MatchNone,
    Not,
    Or,
    Phrase,
    Range,
    Term,
    canonical_hash,
    canonical_json,
)
from searchdsl.executor import execute
from searchdsl.normalize import normalize
from searchdsl.parser import parse
from searchdsl.validate import validate

from oracle import Oracle

SEED = 20260928
N_CASES = 300
RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"


# Fixed generation vocabulary — must be valid against the fixture schema.
TEXT_WORDS = ["fox", "dog", "salmon", "quick", "brown", "lazy", "winter"]
PHRASES = ["quick brown", "lazy dog", "snowy woods"]
KEYWORDS = [("category", "animals"), ("category", "sports"),
            ("tags", "fox"), ("tags", "dog"), ("author", "alice")]
INTS = [1987, 1999, 2001, 2005, 2010, 2015, 2018, 2020, 2021, 2023]
DATES = ["1999-09-09", "2001-03-12", "2010-11-20", "2020-02-29", "2023-04-18"]


class ExprGenerator:
    def __init__(self, rng: random.Random, depth: int = 0, max_depth: int = 3):
        self.rng = rng
        self.depth = depth
        self.max_depth = max_depth

    def leaf(self):
        kind = self.rng.randrange(10)
        if kind < 4:
            # Unfielded text term (default fields title/body).
            return Term(value=self.rng.choice(TEXT_WORDS))
        if kind < 6:
            return Term(value=self.rng.choice(TEXT_WORDS),
                        field_name=self.rng.choice(["title", "body"]))
        if kind == 6:
            return Phrase(value=self.rng.choice(PHRASES))
        if kind == 7:
            field, value = self.rng.choice(KEYWORDS)
            return Term(value=value, field_name=field)
        if kind == 8:
            return Term(value=str(self.rng.choice(INTS)), field_name="year")
        return Term(value=self.rng.choice(DATES), field_name="published")

    def range_leaf(self):
        if self.rng.random() < 0.5:
            lo, hi = sorted(self.rng.sample(INTS, 2))
            low_inc = self.rng.random() < 0.7
            high_inc = self.rng.random() < 0.7
            if not low_inc and not high_inc:
                low_inc = True  # guarantee at least one bound is present
            return Range(field_name="year",
                         gte=str(lo) if low_inc else None,
                         gt=None if low_inc else str(lo),
                         lte=str(hi) if high_inc else None,
                         lt=None if high_inc else str(hi))
        lo, hi = sorted(self.rng.sample(DATES, 2))
        return Range(field_name="published", gte=lo, lte=hi)

    def expr(self):
        if self.depth >= self.max_depth or self.rng.random() < 0.35:
            if self.rng.random() < 0.12:
                return self.range_leaf()
            return self.leaf()
        choice = self.rng.choice(["and", "or", "not", "leaf"])
        child_gen = ExprGenerator(self.rng, self.depth + 1, self.max_depth)
        if choice == "and":
            k = self.rng.randint(2, 3)
            return And(tuple(child_gen.expr() for _ in range(k)))
        if choice == "or":
            k = self.rng.randint(2, 3)
            return Or(tuple(child_gen.expr() for _ in range(k)))
        if choice == "not":
            return Not(child_gen.expr())
        return self.leaf()


def render(node) -> str:
    """Render a generated tree back to DSL source text."""
    if isinstance(node, Term):
        prefix = f"{node.field_name}:" if node.field_name else ""
        return f"{prefix}{node.value}"
    if isinstance(node, Phrase):
        prefix = f"{node.field_name}:" if node.field_name else ""
        return f'{prefix}"{node.value}"'
    if isinstance(node, Range):
        lo = node.gte if node.gte is not None else (node.gt if node.gt else "*")
        hi = node.lte if node.lte is not None else (node.lt if node.lt else "*")
        ob = "[" if node.gte is not None else "{"
        cb = "]" if node.lte is not None else "}"
        return f"{node.field_name}:{ob}{lo} TO {hi}{cb}"
    if isinstance(node, Not):
        inner = node.child
        if isinstance(inner, (And, Or)):
            return f"NOT ({render(inner)})"
        return f"NOT {render(inner)}"
    if isinstance(node, And):
        # Mix explicit and implicit conjunction on purpose.
        return " AND ".join(
            f"({render(c)})" if isinstance(c, Or) else render(c)
            for c in node.children
        )
    if isinstance(node, Or):
        return " OR ".join(
            f"({render(c)})" if isinstance(c, And) else render(c)
            for c in node.children
        )
    if isinstance(node, MatchAll):
        return "()"
    if isinstance(node, MatchNone):
        return "a AND NOT a"
    raise AssertionError(f"cannot render {node!r}")


def _schema_dict(schema):
    return {name: {"type": fs.type, "multi_valued": fs.multi_valued}
            for name, fs in schema.fields.items()}


@pytest.fixture(scope="module")
def oracle(schema, corpus_docs):
    return Oracle(_schema_dict(schema), corpus_docs, list(schema.default_fields))


def test_generated_expressions_preserve_truth(engine, oracle, schema):
    rng = random.Random(SEED)
    log_lines = []
    mismatches = []
    for case_id in range(1, N_CASES + 1):
        gen = ExprGenerator(rng)
        original = gen.expr()
        source = render(original)

        # Parse what we rendered (exercises the real lexer/parser too).
        parsed = parse(source)
        validate(parsed, schema, engine.config.limits)  # must pass

        canon = normalize(parsed)
        canon2 = normalize(canon)
        assert canonical_json(canon) == canonical_json(canon2), \
            f"case {case_id}: normalization not idempotent for {source!r}"

        truth_oracle = oracle.evaluate(parsed)
        truth_canon_oracle = oracle.evaluate(canon)
        truth_exec = set(execute(canon, engine.store, schema).docs)

        ok = truth_oracle == truth_exec == truth_canon_oracle
        log_lines.append(json.dumps({
            "case_id": case_id,
            "source": source,
            "oracle_count": len(truth_oracle),
            "executor_count": len(truth_exec),
            "canon_oracle_count": len(truth_canon_oracle),
            "canonical": canon.to_canonical(),
            "verdict": "match" if ok else "MISMATCH",
        }, ensure_ascii=False, sort_keys=True))
        if not ok:
            mismatches.append((case_id, source,
                               sorted(truth_oracle - truth_exec),
                               sorted(truth_exec - truth_oracle)))

    RUNS_DIR.mkdir(exist_ok=True)
    (RUNS_DIR / "truth-differential.jsonl").write_text(
        "\n".join(log_lines) + "\n", encoding="utf-8"
    )
    assert not mismatches, f"truth mismatches: {mismatches[:5]}"


def test_rerendered_canonical_text_has_same_hash(engine, oracle, schema):
    rng = random.Random(SEED ^ 0x5A5A)
    for _ in range(50):
        node = ExprGenerator(rng).expr()
        source = render(node)
        canon = normalize(parse(source))
        # Re-render canonical leaves to text via a generic printer and
        # re-parse; hash must be stable.
        again = normalize(parse(render(canon)))
        assert canonical_hash(again) == canonical_hash(canon)
