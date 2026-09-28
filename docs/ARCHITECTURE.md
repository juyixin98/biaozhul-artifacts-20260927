# Architecture

```
                 ┌──────────────────────────────────────────────┐
  HTTP  ───────▶ │ prune/api.py  (FastAPI, request-id, JSON DTO) │
                 └───────────────┬──────────────────────────────┘
                                 │
                 ┌───────────────▼──────────────┐
                 │ prune/service.py             │  orchestration,
                 │ parse → validate cols → plan │  error categories
                 └───────┬───────────────┬──────┘
                         │               │
        ┌────────────────▼──┐      ┌─────▼──────────────────────┐
        │ prune/kernel.py   │      │ prune/reference.py          │
        │ level 1: month    │      │ INDEPENDENT ground truth:   │
        │   candidate span  │      │ reads EVERY file with      │
        │ level 2: file min │      │ pyarrow.compute; never      │
        │   /max/null tri-  │      │ imports the kernel          │
        │   state decisions │      └─────────────────────────────┘
        └────────▲──────────┘
                 │ TableContext (in-memory model)
        ┌────────┴───────────┐
        │ prune/catalog.py   │ SQLite, transactional refresh
        └────────▲───────────┘
                 │ FileStat / ColumnStat
        ┌────────┴────────────┐
        │ prune/parquet_      │ all PyArrow knowledge lives here:
        │ adapter.py          │ dir discovery, row-group stat
        └─────────────────────┘ aggregation, truncation detection

  prune/transforms.py  month_tz transform + inversion (fixed spec/tzdb)
  prune/values.py      canonical ordered domains (int/float/str/bool/datetime)
  prune/models.py      predicate AND/OR tree + parser
  prune/logctx.py      request-correlated structured (JSON) tracing
  prune/config.py      TOML configuration
```

## Two pruning levels

### Level 1 — directory partitions

A partition value is a **transform of the source column**, not the column
itself. Here the transform is `month_tz`: calendar month of `ts` in a fixed
IANA zone, labelled `"YYYY-MM"`. The kernel **inverts** the predicate through
that transform to get a *conservative* set of candidate labels.

Candidates are represented as a closed month-label interval `[lo, hi]` (either
end open) over non-null labels plus a tri-state verdict for the explicit NULL
bucket:

* `GE/GT v` → `[month(v), +∞)`, `LE/LT v` → `(-∞, month(v)]`, `EQ v` →
  `{month(v)}`, `BETWEEN` → the month span of the endpoints, `IN` → the hull
  of months of its members (flagged `exact:false`).
* `AND` intersects intervals (max of lower bounds, min of upper bounds); `OR`
  takes the interval hull (a conservative cover).
* Month index arithmetic is `year*12 + (month-1)`, valid for negative
  (pre-1970) timestamps; labels are compared as labels, **never by comparing a
  bucket index against a literal value**.

Zone boundaries are handled by transforming the literal itself, so e.g. a
`GT` at the last instant of a local month still keeps that month's label.

NULL routing: only `IS NULL` on the partition column can match the `__null__`
bucket (`REQUIRED`); comparisons and `IS NOT NULL` exclude it; predicates on
other columns leave it `POSSIBLE`.

### Level 2 — file statistics

For every file surviving level 1, each leaf is evaluated against that file's
per-column stats to a tri-state verdict:

* **PRUNED** — no row can match; a machine-readable reason is attached
  (`FILE_ABOVE_MAX`, `FILE_BELOW_MIN`, `FILE_NO_NULL`, `FILE_ALL_NULL`,
  `FILE_NE_ALL_EQUAL`, `FILE_IN_NO_MATCH`).
* **KEPT** — a match is possible (or, for non-strict ops, certain).
* **UNKNOWN** — insufficient information; the file is **kept**, and the cause
  is reported under `uncertain` (`FILE_STATS_MISSING`,
  `FILE_STATS_TRUNCATED`, `FILE_NULL_COUNT_UNKNOWN`, `FILE_COLUMN_ABSENT`,
  `FILE_STATS_VERSION_MISMATCH`, `FILE_BOUNDARY_INCONCLUSIVE`).

AND/OR combine with three-valued logic: an AND is disproved if *any* branch is
disproved; an OR only if *every* branch is disproved.

### Sound bounds and truncation

A stored extremum counts as a **sound** bound only when recorded in full:

* `min_truncated` ⇒ no usable lower bound; `max_truncated` ⇒ no usable upper
  bound. There is deliberately **no character-padding trick**: no character
  can be appended to a prefix to guarantee it orders above every longer string
  sharing that prefix, so a possibly-truncated side is treated as unknown and
  the file is retained. The opposite (intact) side remains usable.
* Disjointness is tested first using whichever sound bound exists, so e.g. an
  EQ literal far above a file is still pruned using its intact minimum even if
  the maximum was truncated.
* Strict `GT`/`LT` at exactly the recorded bound cannot prove a more/less
  extreme value exists (`FILE_BOUNDARY_INCONCLUSIVE` → retained); non-strict
  `GE`/`LE` on equality are a definite keep.

## Version pinning

The catalog records, per table, the transform spec version, the IANA tzdb
version, the adapter version and the stats-schema version at refresh time. At
plan time a mismatch against the running code disables partition pruning and
emits a `TRANSFORM_VERSION_MISMATCH` failure, rather than silently inverting a
different transform.

## Metadata transaction

`Catalog.refresh_table` deletes and rewrites all rows for a table inside one
deferred SQLite transaction; a failure (missing directory, unreadable file,
etc.) rolls back to the previous snapshot. Timestamps are stored canonically
as epoch microseconds in JSON so catalog content is host-locale independent.

## Why the reference cannot be fooled by the kernel

`prune/reference.py` never imports `prune.kernel`. It rebuilds the matching
expression independently with `pyarrow.compute`, reads every Parquet file in
full, and computes the set of matching `id`s. Validation compares
`scan(all files)` against `scan(files the plan kept)`:

* any id in the first but not the second is a `MISSED_ROW` correctness defect;
* failures inside the reference itself are categorized `REFERENCE_ERROR`, and
  schema disagreements `SCHEMA_DRIFT`, so a broken checker is never mistaken
  for a passing result.
