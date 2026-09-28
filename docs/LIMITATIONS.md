# Limitations and safety notes

These are deliberate scope boundaries of the current implementation.

1. **One partition transform.** Only `month_tz` (calendar month of a datetime
   column in one fixed IANA zone) ships today. The candidate-derivation
   interface is transform-specific; adding e.g. day buckets or hashing needs a
   new transform with its own (conservative) inversion and a bumped spec
   version.

2. **NULL rows must be routed to the null partition by the writer.** Level 1
   treats the explicit `__null__` directory as the only place where
   partition-column NULLs may live. If an external writer places a NULL-source
   row under a normal month label, an `IS NULL` query will not reach it. The
   writer bundled here (`scripts/make_fixtures.py`) routes correctly; for data
   you do not control, refresh-time validation of label/value consistency is
   not yet implemented (see future work) and such rows should be assumed
   untrusted.

3. **Truncated stats are never "repaired" with padding.** A truncated UTF8
   extremum disables the bound on that side entirely (file kept). This is
   maximally safe but forgoes pruning that a smarter byte-level analysis could
   sometimes justify.

4. **Pruning is a plan, not a reader.** The API returns which files to scan;
   it does not itself execute the analytical query or push row-group filters
   into a query engine. The `/validate` endpoint performs a full scan solely as
   an independent correctness oracle.

5. **Stats reflect refresh time.** Files added, removed, or rewritten after
   `refresh` are not seen until the next refresh. There is no incremental
   watching or content-hash reconciliation yet.

6. **OR precision is intentionally coarse.** A union of candidate months is
   represented as its interval hull (`exact:false`), which can keep months in
   a gap between branches. This never drops candidates; it only saves less.
   Per-file stats still prune files inside over-kept partitions.

7. **Single-process SQLite.** The catalog is a local file DB with one writer
   assumed. It is not a coordination service for concurrent writers or remote
   storage.

8. **Literal typing is strict.** A string value against an int column is a
   `400 BAD_PREDICATE`/type-mismatch rather than an implicit cast; on the stat
   side a value off the declared domain yields `FILE_STATS_TYPE_MISMATCH` and
   the file is kept.

9. **Time inputs.** Naive (zone-less) timestamp literals are interpreted as
   UTC and the assumption is documented at parse time; prefer explicit offsets.
   Epoch integers are accepted with a microseconds-vs-seconds heuristic
   (`|v| >= 1e13` ⇒ microseconds).

10. **Verification scope.** Zero-miss has been demonstrated on the bundled
    synthetic dataset and 40 randomized predicates per test run; it is not a
    proof over arbitrary Parquet writers. Foreign files without modern
    statistics are handled conservatively, but exotic encodings (nested types,
    decimals, unsigned-overflow edge cases) are outside the tested domains.

## Failure / uncertainty categories

Hard failures (pruning disabled or request rejected) and uncertain conclusions
(file kept) are always reported separately:

* request: `BAD_PREDICATE`, `UNKNOWN_COLUMN`, `UNKNOWN_TABLE`,
  `NOT_REFRESHED`, `UNKNOWN_ID_COLUMN`
* partition: `PARTITION_OUTSIDE_CANDIDATES`, `PARTITION_NULL_EXCLUDED`,
  `TRANSFORM_VERSION_MISMATCH`
* file uncertain: `FILE_STATS_MISSING`, `FILE_STATS_TRUNCATED`,
  `FILE_STATS_TYPE_MISMATCH`, `FILE_NULL_COUNT_UNKNOWN`,
  `FILE_COLUMN_ABSENT`, `FILE_STATS_VERSION_MISMATCH`,
  `FILE_BOUNDARY_INCONCLUSIVE`
* file pruned: `FILE_ALL_NULL`, `FILE_NO_NULL`, `FILE_ABOVE_MAX`,
  `FILE_BELOW_MIN`, `FILE_NE_ALL_EQUAL`, `FILE_IN_NO_MATCH`
* validation: `MISSED_ROW`, `REFERENCE_ERROR`, `SCHEMA_DRIFT`, `PLAN_ERROR`
