"""Core kernel data model (adapter-independent).

Rows are represented with two *independent* structures:

* ``indices``  -- local dictionary codes; at NULL rows it holds
  ``policy.NULL_SENTINEL`` and is never dereferenced;
* ``valid``    -- a per-row validity bitmap (tuple of bool), the sole
  carrier of NULL semantics.

Thus NULL occupies no dictionary value code and no ordinary value slot.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BatchInput:
    batch_id: str
    # Declared local dictionary, positionally indexed: values[code] is the
    # value bound to local code ``code``. Repeated values are allowed here;
    # the merge step unifies them.
    values: tuple[object, ...]
    # Per-row local codes (NULL_SENTINEL on null rows).
    indices: tuple[int, ...]
    # Per-row validity bits; False means the row is NULL regardless of index.
    valid: tuple[bool, ...]

    def __post_init__(self) -> None:
        if len(self.indices) != len(self.valid):
            raise ValueError("indices and valid must have the same length")


@dataclass(frozen=True)
class BatchStats:
    batch_id: str
    row_count: int
    null_rows: int
    declared_entries: int       # length of the declared local dictionary
    distinct_values: int        # distinct values actually declared
    used_entries: int           # distinct local codes referenced by valid rows
    unused_declared: int        # declared entries never referenced
    duplicate_declared: int     # declared - distinct (repeated dict items)


@dataclass(frozen=True)
class BatchRemap:
    batch_id: str
    # For every declared local code -> global code (or None if the declared
    # slot is... never None: every declared value is in the global dict).
    local_to_global: tuple[int, ...]
    # Remapped per-row global codes with independent validity bitmap.
    global_indices: tuple[int, ...]
    valid: tuple[bool, ...]
    stats: BatchStats


@dataclass(frozen=True)
class GlobalEncoding:
    sort_policy: str
    width_policy: str
    target_width: int
    global_index_width: int
    global_types: tuple[str, ...]
    global_values: tuple[object, ...]
    cardinality: int
    batches: tuple[BatchRemap, ...]

    def global_dict(self) -> list[tuple[str, object]]:
        return list(zip(self.global_types, self.global_values))
