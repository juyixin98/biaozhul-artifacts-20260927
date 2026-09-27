"""Emit canonical AST + error-location artifacts for the ambiguous examples.

Run:  python scripts/ambiguous_examples.py
Writes examples/ambiguous-examples.json (committed for review).

Every successful case records both the *raw parse tree* (showing how the
ambiguity was resolved structurally) and the *canonical tree*; every
failure case records the stable error code plus the exact [start,end)
character span. Values are produced by the parser, but the expected
*interpretation* for these cases is also asserted in
tests/test_parser.py so they cannot silently change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from searchdsl.astnodes import canonical_hash, canonical_json  # noqa: E402
from searchdsl.config import load_config  # noqa: E402
from searchdsl.errors import SearchDSLError  # noqa: E402
from searchdsl.normalize import normalize  # noqa: E402
from searchdsl.parser import parse  # noqa: E402
from searchdsl.search import SearchEngine  # noqa: E402

_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        cfg = load_config(str(ROOT / "config.json"))
        _ENGINE = SearchEngine(cfg)
    return _ENGINE

# (query, why it is ambiguous, human-declared intended reading)
SUCCESS_CASES = [
    ("quick brown fox",
     "adjacent words: implicit AND or a phrase?",
     "implicit AND of three terms (only double quotes make a phrase)"),
    ("a AND b OR c",
     "does AND or OR bind tighter?",
     "(a AND b) OR c — AND binds tighter than OR"),
    ("a b OR c d",
     "mixing implicit conjunction with OR",
     "(a AND b) OR (c AND d)"),
    ("NOT a b",
     "does NOT cover the rest of the line?",
     "(NOT a) AND b — NOT binds tighter than AND"),
    ("NOT a OR b",
     "is the negation over the whole disjunction?",
     "(NOT a) OR b"),
    ('"a AND b OR c"',
     "are the quoted words operators?",
     "one phrase token; symbols inside quotes are literal"),
    ("and or not",
     "are lowercase and/or/not keywords?",
     "three ordinary terms (only uppercase reserved words are operators)"),
    ("year:[2000 TO 2010]",
     "inclusive or exclusive endpoints?",
     "inclusive both ends: gte 2000, lte 2010"),
    ("year:{2000 TO 2010}",
     "inclusive or exclusive endpoints?",
     "exclusive both ends: gt 2000, lt 2010"),
    ("title:fox tags:dog",
     "two field clauses next to each other",
     "implicit AND of the two field-qualified terms"),
    ("a OR b OR c",
     "left or right associativity?",
     "left-associative, flattened to one OR node in canonical form"),
    ("NOT NOT fox",
     "double negation",
     "fox (double negation eliminated)"),
    ("fox fox fox",
     "repeated identical clauses",
     "single term fox (idempotence)"),
    ("()",
     "empty parentheses",
     "match_all (empty query semantics)"),
    ("body:价格 AND salmon",
     "non-ASCII term and Latin term mixed",
     "AND of a Chinese unigram term with a Latin term"),
]

ERROR_CASES = [
    ("", "blank query", "QUERY_EMPTY"),
    ("a AND", "dangling operator", "UNEXPECTED_TOKEN"),
    ("OR a", "operator with no left operand", "UNEXPECTED_TOKEN"),
    ("(a b", "unclosed parenthesis", "UNBALANCED_PAREN"),
    ("a ) b", "closing parenthesis without opener", "UNBALANCED_PAREN"),
    ('"abc', "unterminated phrase", "UNTERMINATED_STRING"),
    ("year:[2000 2010]", "missing TO in range", "RANGE_MALFORMED"),
    ("title:", "field qualifier without value", "UNEXPECTED_TOKEN"),
    ("title :fox", "space before ':' breaks the field qualifier", "UNEXPECTED_TOKEN"),
    ("year:[2010 TO 2000]", "inverted range", "RANGE_EMPTY"),
]


def main() -> int:
    ok_out = []
    for query, ambiguity, resolution in SUCCESS_CASES:
        try:
            parsed = parse(query)
            canon = normalize(parsed)
            ok_out.append({
                "query": query,
                "ambiguity": ambiguity,
                "resolution": resolution,
                "raw_parse_tree": json.loads(canonical_json(parsed)),
                "canonical_tree": json.loads(canonical_json(canon)),
                "query_hash": canonical_hash(canon),
                "status": "ok",
            })
        except SearchDSLError as exc:
            ok_out.append({"query": query, "status": "error", "error": exc.as_dict(),
                           "note": "expected success but failed"})

    err_out = []
    engine = _engine()
    for query, situation, expected_code in ERROR_CASES:
        try:
            from searchdsl.validate import validate

            parsed = parse(query)
            validate(parsed, engine.schema, engine.config.limits)
            err_out.append({"query": query, "situation": situation,
                            "expected_code": expected_code, "status": "ok-unexpected",
                            "raw_parse_tree": json.loads(canonical_json(parsed))})
        except SearchDSLError as exc:
            err_out.append({
                "query": query,
                "situation": situation,
                "expected_code": expected_code,
                "status": "error" if exc.code == expected_code else "WRONG_CODE",
                "actual_code": exc.code,
                "message": exc.message,
                "pos": exc.pos.as_dict() if exc.pos else None,
            })

    payload = {"successful": ok_out, "errors": err_out}
    out = ROOT / "examples" / "ambiguous-examples.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
                   + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(ok_out)} ok, {len(err_out)} error cases)")
    wrong = [e for e in err_out if e["status"] != "error"]
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
