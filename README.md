# Weighted Edit-Distance Spell Correction

A reviewable, multi-module backend that suggests dictionary corrections for a
query using a **weighted unrestricted Damerau edit distance** with insertion,
deletion, substitution and **adjacent-transposition** costs.

Stack: Python 3.12 · FastAPI · SQLite. Everything runs locally with synthetic
fixtures — no external accounts or production data.

## 1. Explicit algorithm decision

There is exactly **one** distance definition, used by the API, the search
heuristic, the pruning bounds and the diagnostics:

> **Weighted adjacent-transposition edit distance** (the *unrestricted
> Damerau* edit-script semantics), computed as a **shortest path over string
> states** with an admissible A* heuristic.

A state is a concrete string; edges are the four edits applicable at every
position — insert, delete, substitute, and swap two **adjacent** characters.
The distance is the minimum total weight of an operation script
source → target. "Unrestricted" means transpositions form chains and a
character may be edited repeatedly:

| pair | this service (unrestricted) | restricted OSA recurrence |
|---|---|---|
| `ca → abc` | **2** (swap + insert) | 3 |
| `abc → bca` | **2** (two adjacent swaps) | 3 |
| `aab → baa` | **2** (two adjacent swaps) | 2 (OSA) / block-table diverges under weights |

### Why not the Lowrance–Wagner *table* recurrence?

This was investigated, not assumed. The familiar LW "last-occurrence crossed
block" recurrence equals the adjacent-swap distance under **unit** costs, but
under **non-uniform/asymmetric swap weights** it can misvalue
repeated-character swap chains. With `swap(ab)=0.2`, `swap(ba)=1.5`,
substitutions `a→b=0.1`, `b→a=0.8`:

- exact `aab → baa` is two adjacent `ab` swaps = **0.4**;
- the LW crossed-block recurrence returns **0.9** or **2.0**.

Because the requirements explicitly combine unrestricted transpositions with
asymmetric costs, the shortest-path *definition* is also the implementation;
the restricted OSA recurrence and the LW table recurrence are deliberately
not used anywhere. This is asserted in `/diagnostics`
(`lowrance_wagner_table_recurrence_used: false`, `restricted_OSA_used:
false`) and in tests.

### Search and the node cap

A* priority is `f = g + h` with the admissible heuristic

```
h(s) = max( |len(s)-len(t)| · min_indel,
            0.5 · L1(char-frequency(s), char-frequency(t)) · min_edit )
```

Non-negative costs make `h` consistent, so the first target pop is optimal.
When a `threshold` is supplied, both the edge path (`g > threshold`) and `f`
prune; returning `distance = inf` then means *exactly* "no script at or below
the threshold exists". A per-candidate `max_search_nodes` cap (default
60 000) bounds pathological permutation searches; if hit, that candidate is
recorded under `uncertainties` rather than guessed. Near spelling errors
(search ≤ ~2 edits, which the dictionary length/multiset bounds pass) expand
only a handful of states.

## 2. Independent reference (test answers are not self-generated)

`tests/reference_oracle.py` shares **no algorithm code** with `app/`. It
recomputes the same distance with its own from-scratch **Dijkstra over
string states**, self-checks its path replay, and supports the same
threshold f-pruning. The differential suite enumerates every pair over small
alphabets and random pairs under six cost models and requires
`app.core == oracle`; the unrestricted-vs-OSA cases are asserted with
independently computed values too.

## 3. Costs and their guarantees

- All four default costs and every per-character override must be finite and
  **non-negative** (`app/config.py` rejects NaN/Inf/negative at startup).
  Non-negativity is what makes both the search and the pruning admissible.
- Substitution is keyed `src:dst`; transposition is keyed on the ordered
  source pair `left:right`. Both are asymmetric.
- Zero costs are supported; when every insertion/deletion is free the length
  window correctly degenerates to "all lengths".
- All threshold/boundary comparisons carry an `EPS = 1e-9` tolerance; a
  candidate exactly at the threshold is kept. (The test suite even catches
  the float-accumulation case where six costs of 0.3 sum to 1.8+3e-16.)

## 4. Edit paths: ordered steps, replayable and recostable

Every candidate carries an **ordered** step list; each step addresses the
string *as it exists at that moment* (`at` = index before applying the step),
which is required because swap chains reuse positions:

```json
{"op":"transpose","at":1,"src":"ie","dst":"ei"}
```

- `core.replay(source, steps)` applies steps in order and verifies each
  precondition (char/pair must match, bounds must be valid).
- `core.recompute_cost(steps, costs)` re-sums weights independently.
- The API reports `path_replay_verified` and `path_recomputed_cost` so each
  distance can be audited without trusting the search.

## 5. Dictionary pruning — lossless at the threshold

Before the exact search, two admissible lower bounds filter SQLite rows:

1. **Length** `| |q|−|c| | · min_indel` → length window in SQL.
2. **Frequency** `0.5 · L1(multiset(q), multiset(c)) · min_edit`
   (one edit changes the L1 distance by at most 2).

A row is dropped only when `max(bound1, bound2) > threshold`. Tests assert
both bounds against the exhaustive oracle on random weighted pairs and, for a
real seeded dictionary, that **every term within threshold survives**
(oracle ground truth over the whole table).

## 6. Stable ranking and deterministic storage

- Candidate rank: `(distance asc, term asc, usage-frequency desc)` — never
  rowid or hash order. SQLite retrieval is `(length, term)`.
- Each import creates an **immutable version**; one version is active; a
  query may pin `version_id`. Versions are never mutated.

## 7. Modules

```
app/
  config.py         Settings, Limits, CostModel (validated non-negativity)
  errors.py         Failure taxonomy (stable codes + HTTP statuses)
  normalization.py  lower → NFKD → strip combining marks; tokenize
  core.py           A* weighted edit search, ordered path, replay, recost
  storage.py        SQLite versioned dictionary
  indexing.py       Admissible bounds, deterministic retrieval, funnel stats
  service.py        Orchestration, ranking, uncertainty aggregation
  api.py            FastAPI, X-Request-ID middleware, structured logs
scripts/seed_db.py  Import a synthetic fixture as an immutable version
tests/
  reference_oracle.py  Independent Dijkstra-over-strings reference
  test_core.py            hand-computed values incl. weighted swap chains
  test_differential_oracle.py  exhaustive + random core↔oracle agreement
  test_pruning.py         admissibility, edges, no-loss over the dictionary
  test_normalization.py / test_storage.py / test_service.py / test_api.py
config/default.json   alphabet, limits, default + per-character costs
examples/             seed_words.txt, example_requests.sh
```

## 8. Limits and explainability

Caps (`config/default.json`): 64 query chars, 8 tokens, 5 000 retrieved
candidates/token, 10 results/token, 60 000 A* nodes/candidate, default
threshold 2.0.

- Hard violations return typed errors (`empty_query`, `query_too_long`,
  `too_many_tokens`, `unsupported_character`, `version_not_found`,
  `invalid_parameter`) with details (character position, cap value…).
- Non-fatal incompleteness is returned separately in `uncertainties`
  (length-window truncation, result truncation, node-cap hit).
- Every response and log line carries the `X-Request-ID` (generated if
  absent), the dictionary version (and whether it is the active one), and a
  per-token funnel: `length_window_rows → rows_retrieved →
  rejected_by_lower_bounds → passed_lower_bounds → within_threshold`.

## 9. Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # exact pins: requirements.lock
python -m scripts.seed_db --activate   # builds data/spellcheck.db
uvicorn app.api:app --reload
```

Endpoints: `GET /health`, `POST /correct`, `GET /versions`,
`GET /diagnostics`; ready-made calls in `examples/example_requests.sh`.

```bash
curl -s -X POST http://127.0.0.1:8000/correct \
  -H 'Content-Type: application/json' -H 'X-Request-ID: demo-001' \
  -d '{"query": "teh recieve", "threshold": 1.5}'
```

## 10. Tests

```bash
pytest                           # full suite (exhaustive differential tests)
pytest tests/test_core.py        # fast algorithm tests
```

## 11. Supported scope and key trade-offs

- Edits are **within one normalized token**; no cross-token transposition or
  word split/merge.
- The exact A* search is fast for near matches but can be expensive for
  arbitrary long permutations of far-apart strings (e.g. complete reversals)
  even though their distance is large. The dictionary length/multiset
  bounds, the threshold f-prune and the node cap bound that work; the cap is
  always reported, never silently truncated into a wrong answer.
- The independent oracle restricts its alphabet to the union of the
  endpoint characters, intermediate length to `|source|+|target|`, and (when
  given) the threshold — all stated and sound for non-negative costs on the
  tested domain; the oracle is test-only.
- Dictionary fixture scale (≈100 synthetic words) is intended for review,
  not as a production index.
