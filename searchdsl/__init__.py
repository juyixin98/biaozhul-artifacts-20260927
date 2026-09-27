"""searchdsl — boolean search DSL with a canonical query tree.

Pipeline: source text -> lexer -> parser -> normalize -> validate -> execute.

Modules:
  spec       field whitelist / text specification
  errors     typed error taxonomy (stable ``code`` values)
  astnodes   query tree node definitions
  lexer      tokenizer (phrases and escapes handled before operators)
  parser     precedence + implicit-AND grammar
  normalize  truth-preserving boolean simplification + canonical ordering
  validate   whitelist, type checking, complexity budget
  analysis   text normalization / tokenization used by index and phrases
  store      versioned SQLite storage and inverted index
  executor   tree evaluation against the index
  diagnostics structured, run-correlated diagnostic collection
  config     standalone configuration loader
  service    FastAPI application
  cli        command-line entry points
"""

__version__ = "1.0.0"

# Versioned protocol identifiers (rule: diagnostics must expose versions).
DSL_SPEC_VERSION = "dsl-1.0"
INDEX_SCHEMA_VERSION = "index-1.0"

from searchdsl.astnodes import (
    Node,
    MatchAll,
    MatchNone,
    Term,
    Phrase,
    Range,
    And,
    Or,
    Not,
    canonical_json,
    canonical_hash,
    clone_node,
    first_pos,
)
from searchdsl.errors import SearchDSLError

__all__ = [
    "__version__",
    "DSL_SPEC_VERSION",
    "INDEX_SCHEMA_VERSION",
    "Node",
    "MatchAll",
    "MatchNone",
    "Term",
    "Phrase",
    "Range",
    "And",
    "Or",
    "Not",
    "canonical_json",
    "canonical_hash",
    "clone_node",
    "first_pos",
    "SearchDSLError",
]
