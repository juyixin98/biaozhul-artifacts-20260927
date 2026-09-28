# Runbook — first-time setup, commands and captured results

All commands are run from the repository root. Python 3.12 on Linux; no
network services or production accounts are used.

## 1. Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 2. Test suite

```bash
$ python -m pytest -q
................................................................................... [100%]
83 passed, 40 deselected in 1.09s
```

The fast suite contains 83 tests; another 40 randomized differential tests
are marked `slow` and run with `python -m pytest -m slow`. They split into:

- `tests/test_crypto.py` — key normalization, leaf/branch/empty domain
  separation, level-by-level empty hashes, HMAC sign/verify;
- `tests/test_kernel.py` — long-shared-prefix structure, delete/restore,
  empty-value ≠ absence, batch-vs-sequential roots, every tamper category;
- `tests/test_service.py` — SQLite persistence, signed journal, historical
  proofs against old roots, reopening a database;
- `tests/test_replay.py` — offline replay acceptance and each rejection class;
- `tests/test_api.py` — real FastAPI app over HTTP (updates, proofs, verify,
  historical roots, 400/409/422, correlation ids, compressed vs raw proofs);
- `independent_tests/test_cross_validation.py` — production output compared
  against known answers built by the **independent** reference code, with
  proofs checked by both verifiers in both directions;
- `independent_tests/test_differential_fuzz.py` — 40 randomized
  insert/overwrite/delete sequences (marked `slow`) where roots and every
  proof must agree with the independent reference after each operation.
  against known answers built by the **independent** reference code, with
  proofs checked by both verifiers in both directions.

To regenerate the known-answer vectors with the independent implementation:

```bash
$ python independent_tests/generate_vectors.py
wrote vectors.json: .../independent_tests/vectors.json
root ka_kb_kc: e7d7ebde7592b3af48fe6c5bb7de517b5c2ff9fe5f543fb18fef3c4ac452ca2c
```

## 3. Synthetic fixture (local data)

```bash
$ PYTHONPATH=src python -m smt.tools.seed --db data/runtime/seed.db --out data/sample
{
  "journal": "data/sample/journal.json",
  "manifest": "data/sample/manifest.json",
  "db": "data/runtime/seed.db",
  "final_root": "e7d7ebde7592b3af48fe6c5bb7de517b5c2ff9fe5f543fb18fef3c4ac452ca2c"
}
```

The fixture keys `…ab00` and `…ab01` share a **248-bit prefix**; `ff…`
diverges at bit 0. The manifest records the staged roots:

```
empty              02843427f172cd96
after_batch        e7d7ebde7592b3af
after_delete_ka    e2a020f59c4a9fe1
after_reinsert_ka  e7d7ebde7592b3af   <- delete+reinsert restores the exact root
final_equals_batch_root = True
```

## 4. Offline replay (no server)

Valid journal — every HMAC checks and every recomputed root matches:

```bash
$ PYTHONPATH=src python -m smt.tools.replay --journal data/sample/journal.json
{
  "result": "accepted",
  "applied": 5,
  "skipped_noop": 0,
  "seq_range": [1, 5],
  "final_root": "e7d7ebde7592b3af48fe6c5bb7de517b5c2ff9fe5f543fb18fef3c4ac452ca2c",
  "signed_final_root": "e7d7ebde7592b3af48fe6c5bb7de517b5c2ff9fe5f543fb18fef3c4ac452ca2c",
  "root_matches": true,
  "nodes_rebuilt": 261
}
OK: journal replay verified
```

Tamper with a value (signature left intact) — rejected with the record `seq`,
the category and a redacted key, exit code 1:

```
$ python -m smt.tools.replay --journal /tmp/journal_tampered.json
{
  "result": "rejected",
  "category": "bad_signature",
  "seq": 1,
  "detail": "HMAC verification failed: record was tampered with or key differs",
  "state": { "key_hex": "00000000…" }
}
```

Reordering/dropping records yields `sequence_gap`/`chain_break`; a replayer
with a different key gets `bad_signature` at seq 1.

## 5. HTTP service

```bash
SMT_SQLITE_PATH=data/runtime/api.db PYTHONPATH=src \
  uvicorn smt.api.app:create_app --factory --host 127.0.0.1 --port 8090
```

Captured against a running server:

```
GET /api/v1/health
  {"status":"ok","spec":"smt-v1","revision":1,
   "root":"02843427f172cd963bf65b49f9c851a311ce3cec574338620e4fb6f9064d25ac"}

POST /api/v1/updates          (unordered body: ff.., ..ab00, ..ab01)
  revision 2
  root e7d7ebde7592b3af48fe6c5bb7de517b5c2ff9fe5f543fb18fef3c4ac452ca2c
  effect order ['00','01','ff']        <- deterministic ascending-key order

GET /api/v1/values/..ab00     -> exists True, value alpha
GET /api/v1/proofs/..ab00     -> exists True, terminal_depth 256, 3 entries,
                                  one empty_run{depth:1,length:254}
POST /api/v1/proofs/verify    -> {"valid":true,"verdict":"valid"}
same proof, root set to ff..  -> {"valid":false,"verdict":"root_mismatch"}
GET /api/v1/proofs/..ab02     -> exists False, terminal empty, depth 255
duplicate key in one batch    -> HTTP 409, category duplicate_key
X-Request-ID: demo-corr-42    -> echoed in body and x-request-id header
GET /api/v1/revisions         -> 2 batch:3 / 1 genesis empty root
```

Historical proof: append `?root=<old-root-hex>` to `/values` or `/proofs`;
old roots are immutable in the node store and continue to verify after the
current root advances (covered by `test_old_root_still_verifiable_after_later_updates`).

## 6. Reading a failure

Every rejection is a JSON envelope

```json
{ "request_id": "…", "error": { "category": "…", "message": "…" } }
```

Categories and what they mean:

| category | meaning |
| --- | --- |
| `malformed_input` | key/value not 32-byte hex etc. (HTTP 400) |
| `duplicate_key` | same key twice in one batch (HTTP 409) |
| `unknown_root` | proof/get for a root this store has no nodes for — **undecidable**, not "absent" (HTTP 404) |
| `malformed` | proof structure cannot be parsed |
| `key_mismatch` | membership terminal binds another key |
| `prefix_mismatch` | non-membership witness leaf is off the queried key's path |
| `terminal_invalid` | `exists` flag contradicts the terminal |
| `step_invalid` | compressed path gap/overlap/overshoot while expanding |
| `root_mismatch` | recomputed root ≠ claimed root (or tampered value/sibling) |
| `bad_signature` / `sequence_gap` / `chain_break` | journal replay failures |

Structured logs (default `SMT_LOG_FORMAT=json`) include the `request_id`,
category, root prefix and revision. Values are never logged; keys appear only
as a short prefix, e.g. `00000000…(64 hex chars)`, and values only as
`{"present": true, "byte_length": 5}`.
