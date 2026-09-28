# Independent test oracle for the composite-key MERGE decision engine.
#
# IMPORTANT: this module deliberately imports NOTHING from merge_engine.*.
# It is a from-scratch, dict-based re-implementation of the decision
# semantics used only by tests, so a bug shared by implementation and
# "expected answer" cannot hide behind the same code. It supports exactly the
# spec features exercised by the test suite (composite keys, NULLS NOT
# DISTINCT, AND/OR/NOT/IS NULL/comparisons, first-match rule priority).
#
# It returns plain dictionaries; tests compare these against the engine's
# planned actions.
from __future__ import annotations

from typing import Any


class OracleSpecError(ValueError):
    """Oracle-side contract failure - distinct from any engine exception."""


# --------------------------------------------------------------------- 3VL
def _truth(v: Any) -> Any:
    if v is None:
        return None
    return bool(v)


def _land(*vals: Any) -> Any:
    saw_null = False
    for v in vals:
        b = _truth(v)
        if b is False:
            return False
        if b is None:
            saw_null = True
    return None if saw_null else True


def _lor(*vals: Any) -> Any:
    saw_null = False
    for v in vals:
        b = _truth(v)
        if b is True:
            return True
        if b is None:
            saw_null = True
    return None if saw_null else False


def _lnot(v: Any) -> Any:
    b = _truth(v)
    return None if b is None else not b


# ----------------------------------------------------------- mini-evaluator
# Only the tiny expression subset used by fixtures/tests. Implemented with a
# hand-written recursive descent parser (not eval) to stay independent.
class _Lexer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0

    def skip_ws(self) -> None:
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def peek(self) -> str:
        self.skip_ws()
        return self.text[self.i] if self.i < len(self.text) else ""

    def take_word(self) -> str | None:
        self.skip_ws()
        j = self.i
        while j < len(self.text) and (self.text[j].isalnum() or self.text[j] in "_."):
            j += 1
        if j == self.i:
            return None
        word = self.text[self.i : j]
        self.i = j
        return word

    def take_string(self) -> str | None:
        self.skip_ws()
        if self.peek() != "'":
            return None
        self.i += 1
        out = []
        while self.i < len(self.text):
            c = self.text[self.i]
            if c == "'":
                if self.i + 1 < len(self.text) and self.text[self.i + 1] == "'":
                    out.append("'")
                    self.i += 2
                    continue
                self.i += 1
                return "".join(out)
            out.append(c)
            self.i += 1
        raise OracleSpecError("unterminated string")

    def take_number(self) -> float | int | None:
        self.skip_ws()
        j = self.i
        while j < len(self.text) and (self.text[j].isdigit() or self.text[j] == "."):
            j += 1
        if j == self.i:
            return None
        token = self.text[self.i : j]
        self.i = j
        return float(token) if "." in token else int(token)

    def take_op(self) -> str | None:
        self.skip_ws()
        for op in (">=", "<=", "<>", "!=", "==", "=", ">", "<", "+", "-"):
            if self.text.startswith(op, self.i):
                self.i += len(op)
                return op
        return None

    def consume(self, literal: str) -> bool:
        self.skip_ws()
        if self.text.startswith(literal, self.i):
            self.i += len(literal)
            return True
        return False


class _Evaluator:
    def __init__(self, text: str, srow: dict[str, Any], trow: dict[str, Any] | None) -> None:
        self.lex = _Lexer(text)
        self.srow = srow
        self.trow = trow

    def eval(self) -> Any:
        value = self._or()
        self.lex.skip_ws()
        if self.lex.i != len(self.lex.text):
            raise OracleSpecError(f"trailing tokens in {self.lex.text!r} at {self.lex.i}")
        return value

    def _or(self) -> Any:
        value = self._and()
        while True:
            save = self.lex.i
            word = self.lex.take_word()
            if word and word.lower() == "or":
                value = _lor(value, self._and())
            else:
                if word is not None:
                    self.lex.i = save
                return value

    def _and(self) -> Any:
        value = self._not()
        while True:
            save = self.lex.i
            word = self.lex.take_word()
            if word and word.lower() == "and":
                value = _land(value, self._not())
            else:
                if word is not None:
                    self.lex.i = save
                return value

    def _not(self) -> Any:
        save = self.lex.i
        word = self.lex.take_word()
        if word and word.lower() == "not":
            return _lnot(self._not())
        if word is not None:
            self.lex.i = save
        return self._comparison()

    def _comparison(self) -> Any:
        left = self._primary()
        # IS [NOT] NULL
        save = self.lex.i
        word = self.lex.take_word()
        if word and word.lower() == "is":
            negate = False
            w2 = self.lex.take_word()
            if w2 and w2.lower() == "not":
                negate = True
                w2 = self.lex.take_word()
            if not w2 or w2.lower() != "null":
                raise OracleSpecError("expected NULL after IS")
            result = left is None
            return (not result) if negate else result
        if word is not None:
            self.lex.i = save
        op = self.lex.take_op()
        if op is None:
            return left
        right = self._primary()
        if left is None or right is None:
            return None
        if op in ("=", "=="):
            return left == right
        if op in ("<>", "!="):
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
        raise OracleSpecError(f"unsupported op {op}")

    def _primary(self) -> Any:
        s = self.lex.take_string()
        if s is not None:
            return s
        num = self.lex.take_number()
        if num is not None:
            return num
        word = self.lex.take_word()
        if word is None:
            if self.lex.consume("("):
                v = self._or()
                if not self.lex.consume(")"):
                    raise OracleSpecError("missing )")
                return v
            raise OracleSpecError("expected value")
        low = word.lower()
        if low == "null":
            return None
        if low == "true":
            return True
        if low == "false":
            return False
        if low.startswith("s."):
            return self.srow[word[2:]]
        if low.startswith("t."):
            if self.trow is None:
                raise OracleSpecError("T referenced on NOT MATCHED side")
            return self.trow[word[2:]]
        # bare column: prefer source then target
        if word in self.srow:
            return self.srow[word]
        if self.trow is not None and word in self.trow:
            return self.trow[word]
        raise OracleSpecError(f"unknown identifier {word!r}")


def oracle_eval(expr: str, srow: dict[str, Any], trow: dict[str, Any] | None) -> Any:
    return _Evaluator(expr, srow, trow).eval()


# -------------------------------------------------------------- assignment
def oracle_assignment(expr: str, srow: dict[str, Any], trow: dict[str, Any] | None) -> Any:
    """Assignments in fixtures are either literals, bare S.x/T.x or
    T.x + S.x; the evaluator supports literals/columns/arithmetic via
    comparison-level parsing, so handle the additive common case explicitly.
    """
    # Fast paths for the forms used in fixtures.
    text = expr.strip()
    if text.upper().startswith("'") and text.endswith("'"):
        inner = text[1:-1].replace("''", "'")
        return inner
    if text in ("null", "NULL"):
        return None
    if (" + " in text) or (" - " in text):
        op = " + " if " + " in text else " - "
        left, right = text.split(op, 1)
        lv = oracle_eval(left.strip(), srow, trow)
        rv = oracle_eval(right.strip(), srow, trow)
        if lv is None or rv is None:
            return None
        return lv + rv if op == " + " else lv - rv
    return oracle_eval(text, srow, trow)


# ---------------------------------------------------------------- decision
def oracle_plan(spec: dict[str, Any], source: list[dict[str, Any]],
                target: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the expected action set independently.

    Returns {"actions": [{"outcome","key","target_rowid","new_values",
    "source_index"}], "duplicate_source": [...], "duplicate_target": [...]}.
    Duplicate conflicts are reported rather than raised so tests can assert
    the engine rejects the same groups.
    """
    keys = list(spec["key_columns"])

    def kof(row: dict[str, Any]) -> tuple:
        return tuple(row.get(k) for k in keys)

    # grouping under NULLS NOT DISTINCT only (tests pass that policy here)
    if spec.get("null_policy", "NULLS_NOT_DISTINCT") != "NULLS_NOT_DISTINCT":
        raise OracleSpecError("oracle fixtures always use NULLS_NOT_DISTINCT")

    src_groups: dict[tuple, list[int]] = {}
    for i, row in enumerate(source):
        src_groups.setdefault(kof(row), []).append(i)
    dup_source = [
        {"key": list(k), "source_indexes": sorted(idx)}
        for k, idx in sorted(src_groups.items(), key=lambda kv: _sortkey(kv[0]))
        if len(idx) > 1
    ]

    tgt_groups: dict[tuple, list[int]] = {}
    for pos, row in enumerate(target):
        tgt_groups.setdefault(kof(row), []).append(pos)
    dup_target = [
        {"key": list(k), "target_positions": sorted(pos)}
        for k, pos in sorted(tgt_groups.items(), key=lambda kv: _sortkey(kv[0]))
        if len(pos) > 1
    ]

    actions: list[dict[str, Any]] = []
    match = {k: pos[0] for k, pos in tgt_groups.items() if len(pos) == 1}

    # deterministic source order independent of input ordering
    order = sorted(range(len(source)), key=lambda i: (_sortkey(kof(source[i])), i))
    for i in order:
        srow = source[i]
        key = kof(srow)
        pos = match.get(key)
        matched = pos is not None
        trow = target[pos] if pos is not None else None
        fired = None
        for order_idx, clause in enumerate(spec["when_clauses"]):
            is_matched_clause = bool(clause["matched"])
            if is_matched_clause != matched:
                continue
            cond = clause.get("condition")
            result = True if cond is None else _truth(oracle_eval(cond, srow, trow))
            if result is not True:
                continue
            fired = order_idx
            action = clause["action"]
            if action == "delete":
                actions.append({
                    "outcome": "DELETE", "source_index": i,
                    "target_rowid": trow.get("__rowid__", pos),
                    "key": list(key), "new_values": {},
                    "fired_clause": fired,
                })
            elif action == "update":
                vals = {
                    col: oracle_assignment(expr, srow, trow)
                    for col, expr in clause["assignments"].items()
                }
                actions.append({
                    "outcome": "UPDATE", "source_index": i,
                    "target_rowid": trow.get("__rowid__", pos),
                    "key": list(key), "new_values": vals,
                    "fired_clause": fired,
                })
            else:
                vals = {k: srow.get(k) for k in keys}
                for col, expr in clause["assignments"].items():
                    vals[col] = oracle_assignment(expr, srow, None)
                actions.append({
                    "outcome": "INSERT", "source_index": i,
                    "target_rowid": None, "key": list(key),
                    "new_values": vals, "fired_clause": fired,
                })
            break
        if fired is None:
            actions.append({
                "outcome": "UNPROCESSED", "source_index": i,
                "target_rowid": trow.get("__rowid__", pos) if trow else None,
                "key": list(key), "new_values": {},
                "fired_clause": None,
            })

    return {
        "actions": actions,
        "duplicate_source": dup_source,
        "duplicate_target": dup_target,
    }


def _sortkey(key: tuple) -> list:
    out = []
    for v in key:
        if v is None:
            out.append((0, ""))
        elif isinstance(v, (int, float)):
            out.append((1, float(v)))
        else:
            out.append((2, str(v)))
    return out
