"""Safe expression compiler for WHEN conditions and assignment expressions.

Expressions are ordinary Python-expression *strings* (so they read like SQL
companion expressions), but they are executed against a hand-written AST
walker - never ``eval`` - with a fixed whitelist:

* literals: numbers, strings, booleans, ``None``
* names:     source/target columns via ``S.col`` / ``T.col`` or bare columns,
             ``true`` / ``false`` / ``null``, ``and``/``or``/``not`` keywords
* operators: arithmetic, comparison, boolean (SQL three-valued logic),
             unary +/-/``~``
* calls:     a small whitelist: coalesce, nullif, upper, lower, length,
             substr/trim, abs/round, cast, case

Compile errors (unknown column/function, disallowed syntax) are InputError at
plan-build time. Runtime errors (type mismatch, division by zero, bad cast)
are collected by the planner and surfaced as COMPUTATION_FAILURE.
"""

from __future__ import annotations

import ast
import operator
from dataclasses import dataclass
from typing import Any, Callable

from .errors import PREDICATE_FAILED_CODE, ComputationFailureError, InputError, SPEC_INVALID_CODE


class PredicateError(Exception):
    """Internal: evaluation-time failure, converted by the planner into a
    batch-level ComputationFailureError carrying row/clause context."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


# SQL three-valued-logic sentinel. Python None already *is* SQL NULL for
# values, but explicit predicates need distinguishable UNKNOWN - which in SQL
# is also NULL. We use None throughout; helpers below implement 3VL.

_BOOL = (bool, int)  # SQLite stores booleans as 0/1


@dataclass(frozen=True)
class Namespace:
    """Resolved column namespace passed to the compiler."""

    source_cols: frozenset[str]
    target_cols: frozenset[str]

    def resolve(self, name: str, *, allow_target: bool) -> str:
        """Return 'S.name' / 'T.name' for a bare column.

        A bare name must exist on exactly one side. If it exists on both, the
        caller must qualify with S./T. - an unqualified ambiguous name is an
        input error rather than a silent choice.
        """
        in_s = name in self.source_cols
        in_t = name in self.target_cols
        if in_s and in_t:
            raise InputError(
                SPEC_INVALID_CODE,
                f"column {name!r} exists in both source and target; "
                "qualify it as S." + name + " or T." + name,
                details={"column": name},
            )
        if in_s:
            return f"S.{name}"
        if in_t:
            if not allow_target:
                raise InputError(
                    SPEC_INVALID_CODE,
                    f"T.{name} cannot be referenced in a NOT MATCHED clause "
                    "(there is no pre-operation target row)",
                    details={"column": name},
                )
            return f"T.{name}"
        raise InputError(
            SPEC_INVALID_CODE,
            f"unknown column {name!r}",
            details={"column": name, "known": sorted(self.source_cols | self.target_cols)},
        )


# ---------------------------------------------------------------- compilation
_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare, ast.IfExp,
    ast.Name, ast.Attribute, ast.Constant,
    ast.Call, ast.And, ast.Or, ast.Not,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Invert,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.Is, ast.IsNot,
    ast.Tuple,
)

_KEYWORD_NAMES = {"true": True, "false": False, "null": None, "none": None, "None": None, "True": True, "False": False}

# Type names used as cast(...) markers; not columns.
_TYPE_NAMES = frozenset({"int", "float", "str", "bool"})

_ALLOWED_FUNCS = frozenset(
    {
        "coalesce", "nullif", "ifnull",
        "upper", "lower", "length", "substr", "substring", "trim", "ltrim", "rtrim",
        "abs", "round", "cast",
        "case",
    }
)


# SQL condition/expression dialect accepted (documented in README):
#   comparisons: = <> != == < <= > >=
#   boolean:     AND OR NOT (case-insensitive, SQL three-valued logic)
#   null tests:  x IS NULL / x IS NOT NULL
#   concat:      a || b
#   strings:     'single quoted' with '' as an embedded quote
# Everything else uses the Python-expression grammar the AST walker accepts.
def sql_to_python_expr(text: str) -> str:
    """Token-level translation from the SQL dialect to a Python expression.

    Rewrites only operators/keywords/string literals; identifiers and numeric
    literals pass through unchanged, so S.col / T.col keep working.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            out.append(ch)
            i += 1
            continue
        if ch == "'":
            # SQL string literal: decode '' escapes, re-emit as a double-quoted
            # Python string (json.dumps gives valid escaping).
            i += 1
            buf: list[str] = []
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    break
                buf.append(text[i])
                i += 1
            if i >= n:
                raise ValueError("unterminated string literal")
            i += 1  # closing quote
            import json as _json

            out.append(_json.dumps("".join(buf)))
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            seen_dot = False
            while j < n and (text[j].isdigit() or (text[j] == "." and not seen_dot)):
                seen_dot = seen_dot or text[j] == "."
                j += 1
            out.append(text[i:j])
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            low = word.lower()
            out.append(
                {
                    "and": "and",
                    "or": "or",
                    "not": "not",
                    "is": "is",
                    "null": "None",
                    "none": "None",
                    "true": "True",
                    "false": "False",
                }.get(low, word)
            )
            i = j
            continue
        # operators
        two = text[i : i + 2]
        if two in ("<=", ">=", "==", "!="):
            out.append(two)
            i += 2
            continue
        if two == "<>":
            out.append("!=")
            i += 2
            continue
        if two == "||":
            out.append("+")
            i += 2
            continue
        if ch == "=":
            out.append("==")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class CompiledExpr:
    """A compiled, namespace-qualified expression. ``text`` is the original
    source text (kept for logs); evaluation goes through the stored AST."""

    text: str
    tree: ast.Expression

    def evaluate(self, env: dict[str, Any]) -> Any:
        return _eval(self.tree.body, env)


def compile_expression(
    text: str,
    namespace: Namespace,
    *,
    allow_target: bool,
    label: str = "expression",
) -> CompiledExpr:
    """Parse + validate an expression string.

    Column qualification is baked in at compile time: every Name/Attribute is
    rewritten into a dotted ``S.col`` / ``T.col`` key looked up in ``env``.
    """
    try:
        translated = sql_to_python_expr(text)
        tree = ast.parse(translated, mode="eval")
    except SyntaxError as exc:
        raise InputError(
            SPEC_INVALID_CODE,
            f"{label} is not a valid expression: {exc.msg}",
            details={"expression": text, "offset": exc.offset},
        )
    except ValueError as exc:
        raise InputError(
            SPEC_INVALID_CODE,
            f"{label} is not a valid expression: {exc}",
            details={"expression": text},
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.expr_context):
            continue  # Load/Store markers, not syntax the user can choose
        if not isinstance(node, _ALLOWED_NODES):
            raise InputError(
                SPEC_INVALID_CODE,
                f"{label}: syntax {type(node).__name__} is not allowed",
                details={"expression": text},
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
                fname = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
                raise InputError(
                    SPEC_INVALID_CODE,
                    f"{label}: function {fname!r} is not in the whitelist "
                    f"{sorted(_ALLOWED_FUNCS)}",
                    details={"expression": text},
                )
            if node.keywords and node.func.id != "case":
                raise InputError(
                    SPEC_INVALID_CODE,
                    f"{label}: keyword arguments are not supported in {node.func.id}",
                )

    _qualify(tree, namespace, allow_target=allow_target, label=label, text=text)
    return CompiledExpr(text=text, tree=tree)


def _qualify(
    tree: ast.Expression,
    ns: Namespace,
    *,
    allow_target: bool,
    label: str,
    text: str,
) -> None:
    """Rewrite column references in place into dotted ``S.col`` / ``T.col``.

    Bare column names become Name nodes whose id is ``"S.<col>"`` / ``"T.<col>"``
    (looked up directly in the eval environment); explicit ``S.col`` /
    ``T.col`` Attribute nodes are validated and evaluated as attributes.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ("substr", "substring", "cast", "case", "round"):
                _check_call_shape(node, label, text)

    for node in ast.walk(tree):
        parent = parents.get(node)

        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name) or node.value.id not in ("S", "T"):
                raise InputError(
                    SPEC_INVALID_CODE,
                    f"{label}: only S.<column> and T.<column> attributes are allowed",
                    details={"expression": text},
                )
            side = node.value.id
            col = node.attr
            bucket = ns.source_cols if side == "S" else ns.target_cols
            if col not in bucket:
                raise InputError(
                    SPEC_INVALID_CODE,
                    f"{label}: unknown column {side}.{col}",
                    details={"expression": text},
                )
            if side == "T" and not allow_target:
                raise InputError(
                    SPEC_INVALID_CODE,
                    f"{label}: T.{col} cannot be used in a NOT MATCHED clause",
                    details={"expression": text},
                )
            continue

        if not isinstance(node, ast.Name):
            continue

        # S / T only valid as the value of an Attribute (validated above).
        if node.id in ("S", "T") and not (
            isinstance(parent, ast.Attribute) and parent.value is node
        ):
            raise InputError(
                SPEC_INVALID_CODE,
                f"{label}: {node.id} must be followed by a column name",
                details={"expression": text},
            )
        if node.id in _KEYWORD_NAMES or node.id in _TYPE_NAMES:
            continue
        if isinstance(parent, ast.Call) and parent.func is node:
            continue  # whitelisted function name
        if isinstance(parent, ast.Attribute) and parent.value is node:
            continue  # S / T handled above

        resolved = ns.resolve(node.id, allow_target=allow_target)
        node.id = resolved


def _check_call_shape(node: ast.Call, label: str, text: str) -> None:
    fid = node.func.id
    if fid == "cast":
        if len(node.args) != 2 or not isinstance(node.args[1], ast.Name):
            raise InputError(
                SPEC_INVALID_CODE,
                f"{label}: cast(value, type_name) expects a value and a type "
                "name of 'int','float','str','bool'",
                details={"expression": text},
            )
        if node.args[1].id not in ("int", "float", "str", "bool"):
            raise InputError(
                SPEC_INVALID_CODE,
                f"{label}: unsupported cast type {node.args[1].id!r}",
                details={"expression": text},
            )
    elif fid == "case":
        # case(cond1, val1, cond2, val2, ..., else_value) - odd arg count
        if len(node.args) < 3 or len(node.args) % 2 == 0:
            raise InputError(
                SPEC_INVALID_CODE,
                f"{label}: case(when1, then1, ..., else) needs an odd number "
                "of arguments (>= 3)",
                details={"expression": text},
            )
    elif fid in ("substr", "substring"):
        if not 2 <= len(node.args) <= 3:
            raise InputError(
                SPEC_INVALID_CODE,
                f"{label}: {fid}(string, start[, length]) takes 2 or 3 arguments",
                details={"expression": text},
            )


# ---------------------------------------------------------------- evaluation
_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS: dict[type, str] = {
    ast.Eq: "=", ast.NotEq: "<>", ast.Lt: "<", ast.LtE: "<=",
    ast.Gt: ">", ast.GtE: ">=",
}


def _eval(node: ast.AST, env: dict[str, Any]) -> Any:
    method = _EVALUATORS.get(type(node))
    if method is None:
        raise PredicateError(f"unsupported syntax {type(node).__name__}")
    return method(node, env)


def _ev_constant(node: ast.Constant, env: dict[str, Any]) -> Any:
    v = node.value
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    raise PredicateError(f"literal of type {type(v).__name__} is not allowed")


def _ev_name(node: ast.Name, env: dict[str, Any]) -> Any:
    name = node.id
    if name in _KEYWORD_NAMES:
        return _KEYWORD_NAMES[name]
    value = env.get(name)
    if value is None and name not in env:
        raise PredicateError(f"unresolved name {name!r}")
    return value


def _ev_attribute(node: ast.Attribute, env: dict[str, Any]) -> Any:
    # Surviving Attribute nodes are S.col / T.col (others rewritten to Names).
    key = f"{node.value.id}.{node.attr}"  # type: ignore[attr-defined]
    if key not in env:
        raise PredicateError(f"unresolved column {key}")
    return env[key]


def _as_bool(v: Any) -> Any:
    """SQL truth test -> True/False/None(UNKNOWN)."""
    if v is None:
        return None
    if isinstance(v, (bool, int, float)):
        return bool(v)
    raise PredicateError(f"value of type {type(v).__name__} is not boolean")


def _ev_boolop(node: ast.BoolOp, env: dict[str, Any]) -> Any:
    is_and = isinstance(node.op, ast.And)
    # SQL 3VL:
    # AND: false if any false; else null if any null; else true
    # OR : true  if any true;  else null if any null; else false
    result: Any = True if is_and else False
    saw_null = False
    for sub in node.values:
        b = _as_bool(_eval(sub, env))
        if b is None:
            saw_null = True
        elif is_and and b is False:
            return False
        elif not is_and and b is True:
            return True
    return None if saw_null else result


def _ev_unaryop(node: ast.UnaryOp, env: dict[str, Any]) -> Any:
    v = _eval(node.operand, env)
    if v is None:
        return None
    try:
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        if isinstance(node.op, ast.Not):
            return None if v is None else not _as_bool(v)
        if isinstance(node.op, ast.Invert):
            return ~v
    except (TypeError, ArithmeticError) as exc:
        raise PredicateError(str(exc))
    raise PredicateError(f"unsupported unary operator {type(node.op).__name__}")


def _ev_binop(node: ast.BinOp, env: dict[str, Any]) -> Any:
    a = _eval(node.left, env)
    b = _eval(node.right, env)
    if a is None or b is None:
        return None  # NULL propagates through arithmetic
    op_type = type(node.op)
    if op_type in (ast.Add,) and isinstance(a, str) and isinstance(b, str):
        return a + b
    fn = _BIN_OPS.get(op_type)
    if fn is None:
        raise PredicateError(f"unsupported operator {op_type.__name__}")
    try:
        return fn(a, b)
    except ZeroDivisionError:
        raise PredicateError("division by zero")
    except TypeError as exc:
        raise PredicateError(f"type error in arithmetic: {exc}")


def _ev_compare(node: ast.Compare, env: dict[str, Any]) -> Any:
    left = _eval(node.left, env)
    result: Any = True
    saw_null = False
    for op, comparator in zip(node.ops, node.comparators):
        right = _eval(comparator, env)
        r = _compare(left, type(op), right)
        left = right
        if r is None:
            saw_null = True
        elif r is False:
            return False
    return None if saw_null else result


def _compare(a: Any, op_type: type, b: Any) -> Any:
    # The translator only produces `is None` / `is not None` (NULL tests);
    # identity on non-None is still evaluated consistently as false.
    if op_type is ast.Is:
        return a is None and b is None
    if op_type is ast.IsNot:
        return not (a is None and b is None)
    if a is None or b is None:
        return None
    try:
        eq = a == b
    except TypeError as exc:
        raise PredicateError(f"cannot compare {type(a).__name__} and {type(b).__name__}: {exc}")
    if op_type is ast.Eq:
        return bool(eq)
    if op_type is ast.NotEq:
        return not bool(eq)
    try:
        if op_type is ast.Lt:
            return a < b
        if op_type is ast.LtE:
            return a <= b
        if op_type is ast.Gt:
            return a > b
        if op_type is ast.GtE:
            return a >= b
    except TypeError as exc:
        raise PredicateError(f"cannot order {type(a).__name__} and {type(b).__name__}: {exc}")
    raise PredicateError("unsupported comparison")


def _ev_ifexp(node: ast.IfExp, env: dict[str, Any]) -> Any:
    cond = _as_bool(_eval(node.test, env))
    if cond is True:
        return _eval(node.body, env)
    if cond is False:
        return _eval(node.orelse, env)
    # NULL condition -> NULL result in SQL's NULLIF-style CASE semantics
    return None


def _ev_tuple(node: ast.Tuple, env: dict[str, Any]) -> Any:
    return tuple(_eval(e, env) for e in node.elts)


def _ev_call(node: ast.Call, env: dict[str, Any]) -> Any:
    fn = node.func.id  # type: ignore[attr-defined]
    args = [_eval(a, env) for a in node.args]
    if fn == "coalesce":
        for a in args:
            if a is not None:
                return a
        return None
    if fn == "ifnull":
        return args[0] if args[0] is not None else args[1]
    if fn == "nullif":
        return None if _sql_eq(args[0], args[1]) else args[0]
    if fn in ("upper", "lower"):
        s = args[0]
        if s is None:
            return None
        _require_str(s, fn)
        return s.upper() if fn == "upper" else s.lower()
    if fn == "length":
        s = args[0]
        if s is None:
            return None
        _require_str(s, fn)
        return len(s)
    if fn in ("substr", "substring"):
        s, start = args[0], args[1]
        if s is None or start is None:
            return None
        _require_str(s, fn)
        length = args[2] if len(args) == 3 else None
        # SQL substr is 1-based; 0 is treated as 1, negatives count from end.
        py_start = max(start, 1) - 1 if start > 0 else len(s) + start
        if length is None:
            return s[py_start:]
        return s[py_start : py_start + length]
    if fn in ("trim", "ltrim", "rtrim"):
        s = args[0]
        if s is None:
            return None
        _require_str(s, fn)
        chars = args[1] if len(args) > 1 else " "
        if fn == "trim":
            return s.strip(chars)
        return s.lstrip(chars) if fn == "ltrim" else s.rstrip(chars)
    if fn == "abs":
        x = args[0]
        return None if x is None else abs(x)
    if fn == "round":
        x = args[0]
        if x is None:
            return None
        ndigits = args[1] if len(args) > 1 else 0
        return round(x, ndigits)
    if fn == "cast":
        return _cast(args[0], node.args[1].id)  # type: ignore[attr-defined]
    if fn == "case":
        # args: pairs + trailing else
        for i in range(0, len(args) - 1, 2):
            if _as_bool(args[i]) is True:
                return args[i + 1]
        return args[-1]
    raise PredicateError(f"unknown function {fn}")


def _sql_eq(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return False
    return bool(a == b)


def _require_str(v: Any, fn: str) -> None:
    if not isinstance(v, str):
        raise PredicateError(f"{fn}() expects a string, got {type(v).__name__}")


def _cast(v: Any, type_name: str) -> Any:
    if v is None:
        return None
    try:
        if type_name == "int":
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, (int, float)):
                return int(v)
            return int(str(v).strip())
        if type_name == "float":
            return float(v)
        if type_name == "str":
            return str(v)
        if type_name == "bool":
            if isinstance(v, str):
                low = v.strip().lower()
                if low in ("true", "1", "t"):
                    return True
                if low in ("false", "0", "f", ""):
                    return False
                raise PredicateError(f"cannot cast {v!r} to bool")
            return bool(v)
    except (ValueError, TypeError) as exc:
        raise PredicateError(f"cast to {type_name} failed: {exc}")
    raise PredicateError(f"unsupported cast type {type_name}")


_EVALUATORS = {
    ast.Constant: _ev_constant,
    ast.Name: _ev_name,
    ast.Attribute: _ev_attribute,
    ast.BoolOp: _ev_boolop,
    ast.UnaryOp: _ev_unaryop,
    ast.BinOp: _ev_binop,
    ast.Compare: _ev_compare,
    ast.IfExp: _ev_ifexp,
    ast.Tuple: _ev_tuple,
    ast.Call: _ev_call,
}


def compile_predicate(
    text: str | None,
    namespace: Namespace,
    *,
    allow_target: bool,
    label: str,
) -> CompiledExpr | None:
    if text is None:
        return None
    return compile_expression(text, namespace, allow_target=allow_target, label=label)


def evaluation_failure(
    message: str, *, details: dict[str, Any] | None = None
) -> ComputationFailureError:
    return ComputationFailureError(PREDICATE_FAILED_CODE, message, details=details)
