"""Typed error taxonomy.

Every failure gets a stable machine-readable ``code`` so tests can assert
the failure *category*, not just the presence of an exception. Categories
kept intentionally coarse-grained and documented in the README:

  QUERY_EMPTY          blank / whitespace-only input
  QUERY_TOO_LONG       raw input exceeds max_query_bytes
  UNTERMINATED_STRING  opening quote with no closing quote
  UNTERMINATED_ESCAPE  trailing backslash
  UNBALANCED_PAREN     '(' without ')' or ')' without '('
  UNEXPECTED_TOKEN     token where an atom/operator was illegal
  RANGE_MALFORMED      '[ a TO b ]' not parseable, e.g. missing TO
  RANGE_EMPTY          inverted or point-empty interval such as [b TO a]
  FIELD_UNKNOWN        field not on the whitelist
  FIELD_TYPE_MISMATCH  operation not valid for the field type
                        (phrase on int, range on keyword/text, ...)
  VALUE_MALFORMED      int/date literal not parseable
  BUDGET_DEPTH         tree deeper than max_nesting_depth
  BUDGET_CLAUSES       more leaf clauses than max_clauses
  BUDGET_QUERY_TERMS   more analyzed tokens than max_query_terms
  BUDGET_PHRASE_TERMS  one phrase longer than max_phrase_terms
  BUDGET_RESULT_WINDOW offset+limit beyond max_result_window
  INTERNAL             unexpected condition (never mapped to "success")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ErrorLocation:
    start: int
    end: int

    def as_dict(self) -> dict:
        return {"start": self.start, "end": self.end}


class SearchDSLError(Exception):
    """Base class for all DSL failures.

    Attributes:
        code: stable category string (module docstring).
        message: human-readable explanation.
        pos: optional character span in the original query text.
        detail: optional structured extra context.
    """

    code = "INTERNAL"

    def __init__(
        self,
        message: str,
        *,
        pos: Optional[ErrorLocation] = None,
        detail: Optional[dict] = None,
        code: Optional[str] = None,
    ):
        super().__init__(message)
        if code is not None:
            object.__setattr__(self, "code", code)
        self.message = message
        self.pos = pos
        self.detail = detail or {}

    def as_dict(self) -> dict:
        d = {"code": self.code, "message": self.message}
        if self.pos is not None:
            d["pos"] = self.pos.as_dict()
        if self.detail:
            d["detail"] = self.detail
        return d


class QuerySyntaxError(SearchDSLError):
    """Lexer/parser-level failures (codes set per raise site)."""


class EmptyQueryError(SearchDSLError):
    code = "QUERY_EMPTY"


class QueryTooLongError(SearchDSLError):
    code = "QUERY_TOO_LONG"


class ValidationError(SearchDSLError):
    """Whitelist / type / budget failures (codes set per raise site)."""


def make_error(code: str, message: str, *, pos=None, detail=None) -> SearchDSLError:
    """Construct the right exception subclass for a code."""
    syntax_codes = {
        "UNTERMINATED_STRING",
        "UNTERMINATED_ESCAPE",
        "UNBALANCED_PAREN",
        "UNEXPECTED_TOKEN",
        "RANGE_MALFORMED",
        "RANGE_EMPTY",
    }
    validation_codes = {
        "FIELD_UNKNOWN",
        "FIELD_TYPE_MISMATCH",
        "VALUE_MALFORMED",
        "BUDGET_DEPTH",
        "BUDGET_CLAUSES",
        "BUDGET_QUERY_TERMS",
        "BUDGET_PHRASE_TERMS",
    }
    if code in syntax_codes:
        return QuerySyntaxError(message, pos=pos, detail=detail, code=code)
    if code in validation_codes:
        return ValidationError(message, pos=pos, detail=detail, code=code)
    if code == "QUERY_EMPTY":
        return EmptyQueryError(message, pos=pos, detail=detail)
    if code == "QUERY_TOO_LONG":
        return QueryTooLongError(message, pos=pos, detail=detail)
    return SearchDSLError(message, pos=pos, detail=detail, code=code)
