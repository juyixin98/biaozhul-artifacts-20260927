# Test run report

Date: 2026-09-28
Run identity: offline development run in the provided sandbox
(branch `ab/run-20260927T101543Z/opp264-b`).

## What was actually executed

### 1. Byte-compilation — PASSED

```
python3 -m compileall src tests tools   →  COMPILE_OK
```

All production modules and the entire pytest suite compile under Python 3.12.3.

### 2. Dependency-free self-check — PASSED (57/57)

```
python tools/selfcheck.py
SELFCHECK RESULT: passed=57 failed=0
ALL SELF-CHECKS PASSED
```

Saved output: `artifacts/selfcheck.txt`.

The self-check uses only the Python standard library and verifies the
hand-written codec against:

* an **independently written reference Keccak-256** in `tools/selfcheck.py`
  (not the code under test) using published digests `keccak256("")` and
  `keccak256("abc")`;
* **hand-fixed canonical byte vectors** (constants authored independently of
  the decoder): uint/int/uint8/int8 sign extension, bool, address, bytes4,
  empty bytes/string, padded bytes/string, empty dynamic array, non-empty
  dynamic array;
* **round trips** for nested dynamic arrays (`uint256[][]`, `bytes[][]`),
  fixed arrays of dynamic arrays (`uint256[][2]`), arrays of dynamic tuples
  (`(uint256,bytes)[]`), tuples with multiple dynamic siblings
  (`(bytes,uint256[],string)`), deeply nested tuple/array mixes, negative and
  extreme signed integers;
* **selectors** `transfer`/`approve`/`transferFrom` via the reference Keccak
  (`a9059cbb`, `095ea7b3`, `23b872dd`);
* **hostile inputs asserting exact failure categories**: pointer-into-head
  (`offset_out_of_bounds`), duplicate pointer (`offset_overlap`), aligned and
  unaligned pointers past the blob, declared 2^64-1 bytes / 2^60 array count /
  2^256-1 count under a tiny allocation cap (`length_too_large` /
  `offset_out_of_bounds`, no giant allocation), unsigned overflow and signed
  out-of-range (`value_out_of_range`), bool=2 and dirty bytes/string padding
  (`non_canonical_padding`), invalid UTF-8, truncation, trailing bytes, and
  encoder range/arity errors.

Additional ad-hoc probes (mixed static/dynamic siblings, `bytes[]`,
`string[]`, fixed arrays of static and dynamic elements, exact canonical hex
for `transfer(address,uint256)`, `uint256[3]`, and `(uint256,bytes)`) all
encoded and decoded as expected.

## What could NOT be executed here (blocked, not skipped as passing)

The sandbox permission classifier **blocked `pip install`** (a transient
"Stage 2 classifier error" across four attempts), and the base interpreter had
none of the third-party packages installed. Therefore the authoritative
`pytest` suite and the layers that import the mature crypto/HTTP libraries
were **not executed** in this environment. These tests are written and
compiled, and are the ones to run after `bash setup.sh`:

| Suite | Status here | Depends on |
|---|---|---|
| `tests/test_golden_vectors.py` | **NOT EXECUTED** | eth-abi, eth-utils (oracle) |
| `tests/test_selectors.py` | NOT EXECUTED | eth-utils |
| `tests/test_security_malformed.py` | NOT EXECUTED | pytest (logic self-checked) |
| `tests/test_integer_rules.py` | NOT EXECUTED | pytest (logic self-checked) |
| `tests/test_chain_kernel.py` | NOT EXECUTED | eth-keys, coincurve, Crypto |
| `tests/test_replay_storage.py` | NOT EXECUTED | eth-keys, coincurve, Crypto |
| `tests/test_api.py` | NOT EXECUTED | fastapi, httpx, pydantic, crypto |
| `tools/selfcheck.py` | **PASSED 57/57** | stdlib only |

The ABI encode/decode core — the primary deliverable — is therefore verified
by the self-check, but its byte-for-byte equivalence to the mature `eth_abi`
library, the secp256k1 signature kernel, the FastAPI surface, and the SQLite
replay path remain to be confirmed by `pytest` once dependencies install:

```bash
bash setup.sh
pytest -v
```

No test was reported as passing without actually running it; every unexecuted
item is listed above as NOT EXECUTED.

## Bugs found and fixed during development (via the executed self-check)

1. Top-level argument list emitted an extra tuple layer (later confirmed the
   correct behavior: top level is a head/tail sequence with a pointer even for
   a lone dynamic argument).
2. Dynamic arrays were encoded without their length word.
3. Dynamic-array element offsets did not account for the leading length word.
4. Container head sizing used the internal `static_size_words()` of dynamic
   tuples instead of 1 pointer slot, misplacing outer offsets.
5. Decoder bound dynamic child regions to sibling pointers; rewritten as a
   two-pass measure-then-read decoder with per-container relative regions.
6. Pointer validation reordered so into-head/past-end are `offset_out_of_bounds`
   before overlap/gap classification.
