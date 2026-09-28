"""AST node definitions for the supported SQL subset.

The parser (``parser.py``) produces these nodes. Only a deliberately small
surface of SQL is modeled; anything else surfaces as :class:`Unsupported`
rather than being accepted without understanding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .lexer import Span


@dataclass(frozen=True)
class Param:
    """A value placeholder (``?`` / ``$1`` / ``:name`` / ``@name``)."""

    ref: str          # "?" / "1" / "name"
    style: str
    span: Span
    array_context: bool = False  # inside IN (...) or ANY(...)
    tuple_context: bool = False  # one element of a row tuple in IN
    limit_context: bool = False  # LIMIT / OFFSET argument


@dataclass(frozen=True)
class Slot:
    """An identifier slot ``${name}`` — a dynamic identifier, never a value."""

    name: str
    span: Span
    qualifier: str | None = None  # set for qualified forms like ``o.${col}``


@dataclass(frozen=True)
class ColumnRef:
    name: str           # normalized name (unquoted => upper-case)
    qualifier: str | None
    quoted: bool        # came from a quoted identifier (case preserved)
    star: bool = False
    span: Span | None = None


@dataclass(frozen=True)
class TableRef:
    name: str
    qualifier: str | None  # schema qualifier, if any
    quoted: bool
    alias: str | None      # exposed alias (normalized upper)
    span: Span
    slot: Slot | None = None  # set when the table position is a ${slot}
    param: "Param | None" = None  # set when a value placeholder is abused here


@dataclass(frozen=True)
class Literal:
    value: str | int | float | None
    span: Span


@dataclass(frozen=True)
class FuncCall:
    name: str
    args: list["Expr"]
    star: bool
    distinct: bool
    span: Span


@dataclass(frozen=True)
class Unary:
    op: str
    operand: "Expr"
    span: Span


@dataclass(frozen=True)
class Binary:
    op: str
    left: "Expr"
    right: "Expr"
    span: Span


@dataclass(frozen=True)
class InList:
    expr: "Expr"
    items: list["Expr"]
    negated: bool
    span: Span


@dataclass(frozen=True)
class Between:
    expr: "Expr"
    low: "Expr"
    high: "Expr"
    negated: bool
    span: Span


@dataclass(frozen=True)
class IsNull:
    expr: "Expr"
    negated: bool
    span: Span


@dataclass(frozen=True)
class Cast:
    expr: "Expr"
    type_name: str
    span: Span


@dataclass(frozen=True)
class CaseExpr:
    subject: "Expr | None"
    whens: list[tuple["Expr", "Expr"]]
    default: "Expr | None"
    span: Span


Expr = (
    Param | Slot | ColumnRef | Literal | FuncCall | Unary | Binary
    | InList | Between | IsNull | Cast | CaseExpr
)


@dataclass
class OrderItem:
    expr: Expr
    direction: str | None  # "ASC" / "DESC" / None
    nulls: str | None      # "FIRST" / "LAST" / None
    direction_slot: Slot | None = None  # ${kw} used in place of ASC/DESC


@dataclass
class Select:
    kind: str = "select"
    distinct: bool = False
    items: list[tuple[Expr, str | None]] = field(default_factory=list)
    from_tables: list[TableRef] = field(default_factory=list)
    joins: list[tuple[TableRef, Expr | None]] = field(default_factory=list)
    where: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    having: Expr | None = None
    order_by: list[OrderItem] = field(default_factory=list)
    limit: Param | Literal | None = None
    offset: Param | Literal | None = None


@dataclass
class Insert:
    kind: str = "insert"
    table: TableRef | None = None
    columns: list[ColumnRef] = field(default_factory=list)
    rows: list[list[Expr]] = field(default_factory=list)
    from_select: Select | None = None  # INSERT ... SELECT


@dataclass
class Update:
    kind: str = "update"
    table: TableRef | None = None
    assignments: list[tuple[ColumnRef, Expr]] = field(default_factory=list)
    where: Expr | None = None


@dataclass
class Delete:
    kind: str = "delete"
    table: TableRef | None = None
    where: Expr | None = None


Statement = Select | Insert | Update | Delete


@dataclass
class Script:
    statements: list[Statement]
    comment_spans: list[Span] = field(default_factory=list)
    quoted_ident_spans: list[Span] = field(default_factory=list)
