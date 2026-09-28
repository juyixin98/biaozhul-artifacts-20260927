# Test report

Result of the actual runs performed while building this repository (Python
3.12.3, Linux, deps from `requirements.txt`). Re-run everything with:

```bash
python -m pytest                       # full suite, ~80 s on the build machine
python -m pytest tests/test_core.py    # fast algorithm tests only
```

## Final result

| module | tests | status |
|---|---:|---|
| tests/test_core.py | 35 | pass |
| tests/test_differential_oracle.py | 2502 | pass |
| tests/test_pruning.py | 93 | pass |
| tests/test_normalization.py | 8 | pass |
| tests/test_storage.py | 5 | pass |
| tests/test_service.py | 11 | pass |
| tests/test_api.py | 13 | pass |
| **total** | **2667** | **all pass** |

Full-suite wall time: ~78–87 s. The exhaustive differential file alone is
~22 s and deliberately dominates runtime; it contains no answer produced by
the code under test.

Reference answers come from `tests/reference_oracle.py`, an independent
Dijkstra search over concrete string states (own neighbors, own replay
self-check, no shared algorithm code with `app/`).

## Coverage of the requested review cases

- **Shortest-path exhaustive reference on short strings**: every pair over
  `{a,b}` up to length 4 (1089 pairs), every pair over `{a,b,c}` up to
  length 3, six cost models (unit; mixed positive weights; free insertions;
  asymmetric substitution/transposition; per-character tables).
- **Repeated characters**: `aaa/aaaa/aaaaa`, `banana`, `mississippi`,
  `aab↔baa`, `abb↔bba`, `abab↔baba`, … both as known values and oracle
  differential pairs.
- **Transposition chains**: `ca→abc`, `abc→bca`, `aab→baa` asserted against
  the independent oracle and against independently computed restricted-OSA
  values (the unrestricted result must be strictly smaller).
- **Asymmetric costs**: direction-specific substitution (`i→y` 0.5 vs
  `y→i` 1.0), one-sided transposition (`ei→ie` 0.25 vs reverse 1.0),
  weighted swap chains (`aab→baa` = 0.4), delete/insert asymmetry 5.0/1.0.
- **Threshold edges**: candidate at exactly the threshold kept; 1.9 vs
  2.0-ε integer-boundary behaviour; directional length window under unequal
  insert/delete costs; free-cost degenerate windows.
- **Pruning never loses an in-threshold candidate**: for a real seeded
  dictionary the in-threshold set found by the independent oracle over the
  **whole** table is asserted to be a subset of what survives the SQL
  length window + frequency bound.
- **Failure classes** (not just "endpoint works"): `empty_query`,
  `unsupported_character` (with position), `query_too_long`,
  `too_many_tokens`, `version_not_found`, `invalid_parameter`, plus the
  422 schema rejection of a negative threshold.
- **Stable ordering**: repeated identical queries return identical order;
  order is `(distance, term, -frequency)`.

## Failures observed during development and how they were resolved

These were real failures from real runs; the fixes are part of the codebase:

1. **Wrong hand-computed expectation** `seperate→separate = 2`. Actual
   distance is 1 (one substitution). Test corrected; this is exactly why the
   oracle exists independently.
2. **LW table recurrence ≠ adjacent-swap distance under non-uniform swap
   weights** — found by the differential oracle (`aab→baa`: exact 0.4, LW
   block 0.9/2.0). Resolved by replacing the table recurrence with an A*
   shortest path over the four edit operations, so the service has exactly
   one distance definition. Documented in README §1 and `/diagnostics`.
3. **Float accumulation pruned a feasible path** in the bounded oracle
   (six costs of 0.3 summed to 1.8+3e-16 and the `nd <= budget` test
   rejected the delete-all path). Fixed with an explicit EPS tolerance.
4. **Length window floor boundary** dropped candidates exactly at an integer
   multiple of the threshold (`floor(1-ε)=0`). Fixed by adding EPS before
   flooring; asserted by edge tests.
5. **Normalization order** (`NFKC → lower → strip marks`) left precomposed
   `é` unsupported. Reordered to `lower → NFKD → strip Mn`; added an accent
   acceptance test and a non-decomposing symbol rejection test.
6. **Synthetic dictionary contained misspellings** (`teh`, `adress`,
   `recieve`, `seperate`, `definately`, `thier`) and a duplicated line, so
   misspelled queries "exact-matched". Removed/deduplicated; correct forms
   already present.
7. **Unrestricted paths reuse source positions**, so an original-coordinate
   disjoint-span representation cannot express swap chains. Path format
   changed to an ordered step list with per-step preconditions; replay and
   recompute updated and audited.
8. **Direction-aware length bound**: separate `min_delete`/`min_insert`
   window sides replaced the shared `min_indel` window (tighter and correct
   when one direction is free). Asserted by new window tests.

## Not executed / out of scope for this run

- **Third-party library cross-validation**: an attempt to `pip install
  editdistance-s rapidfuzz` for an additional unit-cost cross-check was
  denied by the environment permission layer and was not retried. The
  independent in-repo Dijkstra oracle provides stronger coverage for the
  weighted case anyway; a unit-cost cross-check remains a nice-to-have.
- **Production-scale performance** (dictionaries orders of magnitude larger
  than the ~100-term fixture, latency under load) was not benchmarked; the
  exact A* search plus SQL window is designed for review-scale data. The
  A* node cap (default 60 000) bounds pathological permutation searches and
  is reported under `uncertainties` instead of returning a guessed distance.
- Exact search over long strings under zero-cost models is exponential by
  nature (free insertions create exponentially many zero-cost states); such
  models are covered exhaustively on length ≤ 4 rather than on long pairs.
  No test is silently skipped for this — the scope is stated in the test
  itself and in the README.
