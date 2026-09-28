"""textindex — fixed-Unicode-version text indexing service.

Public package surface (kept small and explicit so the module data/error
contracts are clear):

* errors   — error categories, codes and the TextIndexError hierarchy
* unicode_version — the single source of truth for the pinned data version
* encoding — strict bytes/text validation
* normalizer — text canonicalization
* segmenter — extended grapheme cluster segmentation (mature library)
* index    — our own bidirectional index build/query/serialization
* edits    — our own incremental edit application
* digest   — content digest
* storage  — SQLite versioned persistence
* diagnostics — run ids and structured replay logs
"""

__all__ = [
    "errors",
    "unicode_version",
    "encoding",
    "normalizer",
    "segmenter",
    "index",
    "edits",
    "digest",
    "storage",
    "diagnostics",
]
