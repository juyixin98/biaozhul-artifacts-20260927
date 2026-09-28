"""Data models shared by parser, kernel and API layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    UNANALYZABLE = "unanalyzable"


class Severity(str, Enum):
    ADVISORY = "advisory"
    REJECT = "reject"
    UNANALYZABLE = "unanalyzable"


# Finding codes -> default severity. Keeping the catalogue in one place makes
# the "why accepted / rejected / undecidable" reporting explicit.
REJECT = Severity.REJECT
UNDECIDABLE = Severity.UNANALYZABLE
ADVISORY = Severity.ADVISORY

FINDING_CATALOGUE: dict[str, tuple[Severity, str]] = {
    "LEX_ERROR": (UNDECIDABLE, "Input could not be tokenized as SQL"),
    "PARSE_REGION_UNANALYZABLE": (
        UNDECIDABLE,
        "A syntactic region could not be structurally parsed",
    ),
    "EMPTY_TEMPLATE": (REJECT, "Template contains no SQL statement"),
    "UNRECOGNIZED_STATEMENT": (REJECT, "Statement form is not recognised"),
    "MULTIPLE_STATEMENTS": (REJECT, "More than one statement in a single template"),
    "STATEMENT_TYPE_NOT_ALLOWED": (REJECT, "Statement type is outside the allowed set"),
    "VALUE_PARAM_AS_IDENTIFIER": (
        REJECT,
        "A value bind-parameter (?) is used where an identifier (table/column/sort key) is required",
    ),
    "SLOT_UNDECLARED": (REJECT, "Identifier slot {{name}} is not declared in the policy whitelist"),
    "MISSING_IDENTIFIER": (REJECT, "No identifier value was supplied for a declared slot"),
    "IDENTIFIER_NOT_WHITELISTED": (
        REJECT,
        "Dynamic identifier value is not in the declared whitelist",
    ),
    "MISSING_BINDING": (REJECT, "No value supplied for a bind parameter"),
    "MIXED_PARAMETER_STYLES": (
        REJECT,
        "Anonymous ? and numbered ?NNN markers cannot be mixed in one statement",
    ),
    "UNUSED_BINDING": (ADVISORY, "A supplied binding is never referenced by the template"),
    "ARRAY_PARAM_IN_SCALAR_CONTEXT": (
        REJECT,
        "Array binding can only expand inside an IN (...) list",
    ),
    "EMPTY_EXPANSION": (REJECT, "Array binding is empty; IN () is not valid SQL"),
    "INVALID_PARAM_TYPE": (REJECT, "Bound value has an unsupported type"),
    "PARAM_VALUE_NOT_ALLOWED": (REJECT, "Bound value is outside the parameter allow-list"),
    "UNKNOWN_TABLE": (REJECT, "Static table name is absent from the read-only fixture catalog"),
    "TARGET_TABLE_NOT_WRITABLE": (
        REJECT,
        "Target table is not in the policy writable-tables list",
    ),
    "MISSING_WHERE": (REJECT, "UPDATE/DELETE requires a WHERE clause under this policy"),
}


@dataclass
class Span:
    start: int
    end: int
    line: int
    col: int

    @classmethod
    def from_token(cls, t: Any) -> "Span":
        return cls(t.start, t.end, t.line, t.col)


@dataclass
class ParamUse:
    """A single occurrence of a value bind-parameter."""

    marker: str            # '?' or ':name'
    occurrence: int        # 0-based index across the whole statement
    span: Span
    context: str           # where / values / in_list / limit / ...
    expansion: bool = False  # inside IN (...) -> may take an array


@dataclass
class SlotUse:
    name: str
    occurrence: int
    span: Span
    context: str


@dataclass
class RelationRef:
    name: str | None       # static relation name (last component of a.b); None for dynamic/subquery
    slot_name: str | None  # when relation target is a {{ slot }}
    kind: str              # table | cte | table_function | subquery | param(direct, invalid)
    span: Span
    alias: str | None = None
    components: tuple[str, ...] = ()  # schema-qualified pieces
    note: str | None = None


@dataclass
class StaticColumn:
    name: str
    span: Span
    context: str           # insert_col / set_lhs
    target: str | None     # owning target table when unambiguous


@dataclass
class Statement:
    stmt_type: str  # SELECT | INSERT | UPDATE | DELETE
    relations: list[RelationRef] = field(default_factory=list)
    target: RelationRef | None = None
    params: list[ParamUse] = field(default_factory=list)
    slots: list[SlotUse] = field(default_factory=list)
    insert_columns: list[StaticColumn] = field(default_factory=list)
    set_columns: list[StaticColumn] = field(default_factory=list)
    has_where: bool = False
    compound: bool = False
    trailing_tokens: int = 0  # tokens after complete parse (recovery indicator)


@dataclass
class Finding:
    code: str
    message: str
    span: Span | None = None
    context: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def severity(self) -> Severity:
        return FINDING_CATALOGUE[self.code][0]


@dataclass
class Coverage:
    """What the analyzer *did* and did *not* cover — the honest-limits section."""

    lexed: bool = False
    statement_parsed: bool = False
    statement_type: str | None = None
    relation_checks: list[dict[str, Any]] = field(default_factory=list)
    column_checks: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lexed": self.lexed,
            "statement_parsed": self.statement_parsed,
            "statement_type": self.statement_type,
            "relation_checks": self.relation_checks,
            "column_checks": self.column_checks,
            "skipped": self.skipped,
        }
