# ABI Restricted Backend (Ethereum ABI encode/decode + chain kernel)

A from-scratch, security-oriented implementation of a restricted subset of the
Ethereum ABI head/tail encoding, plus a small signed chain-state kernel,
SQLite-indexed storage, and an offline replay engine. Built in Python with
FastAPI and SQLite. All inputs are **local synthetic fixtures** — there are no
real accounts or external services.

The encoder/decoder in `src/abibackend/abi/` is hand-written. The only
cryptographic primitives are delegated to mature libraries:

* **Keccak-256** → PyCryptodome (`Crypto.Hash.keccak`)
* **secp256k1 sign / recover / address** → `eth-keys` (coincurve / libsecp256k1)
* **Test oracle** → `eth-abi` / `eth-utils`, used *only under `tests/`*, never
  imported by the production code.

---

## Supported range

| Family | Types |
|---|---|
| Integers | `uint8..uint256`, `int8..int256` (step 8), two's complement, sign-extended |
| Booleans / address | `bool` (strict 0/1), `address` (160-bit) |
| Fixed bytes | `bytes1..bytes32` (right-padded, padding must be zero) |
| Dynamic | `bytes`, `string` (UTF-8 enforced) |
| Arrays | `T[k]` fixed, `T[]` dynamic, freely nested |
| Tuples | `(T1,T2,...)`, freely nested with arrays and other tuples |

Nested **dynamic arrays of dynamic types**, **dynamic tuples containing
multiple dynamic siblings**, empty `bytes`/`string`, empty dynamic arrays, and
negative integers are all covered by golden vectors.

### Key design decisions / trade-offs

1. **Relative offsets, per-container regions.** Every dynamic pointer inside a
   container is interpreted relative to that container's own head (and for a
   dynamic array, relative to the array start that includes its length word).
   Nested containers receive their own `[base, end)` region; we never treat an
   inner offset as an absolute file offset.
2. **Two-pass strict decoder.** A measure pass reads only pointer/length words
   to determine each dynamic body's exact extent (without allocating payloads);
   a value pass then decodes inside tight regions. This cleanly distinguishes a
   genuine overlap from adjacent siblings and rejects gaps (non-canonical
   layout).
3. **Bounded allocation.** A declared `bytes` length or array element count is
   checked against `ABI_MAX_ALLOC_BYTES` (default 8 MiB) with checked
   arithmetic **before** any `bytes`/list allocation. A hostile
   `0xFFFF…` length or `count*32` integer overflow raises `length_too_large`
   rather than triggering a huge allocation.
4. **Canonicality, not mere parsability.** The decoder rejects non-canonical
   integer sign extension, dirty high/low padding, booleans other than 0/1,
   non-UTF-8 strings, unaligned/backwards/duplicate pointers, gaps, trailing
   bytes, and top-level blobs that are not exactly consumed.
5. **Typed failure categories.** Each failure is a distinct exception
   (`offset_out_of_bounds`, `offset_overlap`, `length_too_large`,
   `non_canonical_padding`, `non_canonical_encoding`, `trailing_bytes`,
   `value_out_of_range`, …). The HTTP layer returns the specific code; unknown
   errors are never folded into a success.
6. **Synthetic token kernel instead of an EVM.** The chain layer models one
   ERC-20-like contract (`transfer`/`approve`/`transferFrom`) with secp256k1
   signatures over a canonical ABI-encoded preimage, nonce and balance/allowance
   checks, and a deterministic Keccak state root. This exercises the codec and
   signature path end-to-end without pulling in a full VM.

### Explicitly out of scope (restricted subset)

`fixed`/`ufixed` point-math, function external-types, user-defined value types,
explicit tuple component names in wire output (names are not part of ABI
encoding; tuples are positional), packing (`abi.encodePacked`), and real EVM
execution.

---

## Project layout

```
src/abibackend/
  config.py              independent config layer (env-driven, local defaults)
  log_utils.py           structured JSON logging with run/correlation ids
  abi/                   encoding & verification (the hand-written codec)
    types.py             type headers + canonical parser
    encoder.py           head/tail encoder, relative offsets, selectors
    decoder.py           strict two-pass bounds-checked decoder
    errors.py            typed failure taxonomy
  crypto/                mature Keccak + secp256k1 primitives
  chain/                 chain-state kernel (synthetic token, signatures)
  storage/               SQLite indexed repository (blocks/txs/events/accounts/runs)
  replay/                deterministic fixtures + offline replay engine + CLI
  api.py                 FastAPI app
tests/                   pytest suite (golden vs eth_abi, security, kernel, api)
tools/selfcheck.py       dependency-free stdlib self-check (own reference Keccak)
EXAMPLES.md              curl + Python examples
```

The four engineering layers requested map to:

* **编码与验签 (encode & signature verification)** → `abi/`, `crypto/`, `chain/`
* **链状态内核 (chain-state kernel)** → `chain/kernel.py`
* **索引存储 (indexed storage)** → `storage/repository.py`
* **离线回放 (offline replay)** → `replay/`
* **独立测试与配置层 (independent tests & config)** → `tests/`, `config.py`

---

## Quick start

Requires Python ≥ 3.10.

```bash
bash setup.sh            # creates .venv and installs locked dependencies
source .venv/bin/activate

pytest                   # full test suite
bash run.sh serve        # API on http://127.0.0.1:8080
bash run.sh replay       # offline deterministic scenario -> SQLite + report
```

### Offline / no-dependency check

If the third-party wheels cannot be installed, a standard-library-only
self-check verifies the codec against an independently written reference
Keccak-256 plus hand-fixed canonical vectors and hostile-input categories:

```bash
python tools/selfcheck.py
```

It needs only the Python standard library (does **not** import pycryptodome,
eth-abi, eth-keys, or fastapi). The full `pytest` suite is the authoritative
cross-check against mature libraries and should be run after `bash setup.sh`.

---

## Configuration

All settings are environment variables with safe local defaults
(see `src/abibackend/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `ABI_DB_PATH` | `./data/abibackend.sqlite3` | SQLite file |
| `ABI_API_HOST` / `ABI_API_PORT` | `127.0.0.1` / `8080` | bind address |
| `ABI_MAX_ALLOC_BYTES` | `8388608` (8 MiB) | decode allocation ceiling |
| `ABI_MAX_WORDS` | `1000000` | fixed-array word bound |
| `ABI_CHAIN_ID` | `264` | synthetic chain id |
| `ABI_REPLAY_SEED` | `264` | deterministic key/fixture seed |
| `ABI_LOG_LEVEL` | `INFO` | log level |

---

## Testing & diagnostics

`tests/` contains independent tests that assert concrete values **and failure
categories**, not merely "the interface was callable":

* `test_golden_vectors.py` — hand-fixed canonical bytes **and** byte-for-byte
  comparison against `eth_abi.encode`, plus decode-our-blob/encode-our-decode
  cross-checks. The oracle answers come from `eth_abi`, never from the code
  under test. Covers nested dynamic arrays, empty strings/bytes, negative ints,
  multibyte UTF-8, and rich tuple/array nesting.
* `test_security_malformed.py` — offset-into-head, duplicate/backwards
  pointers, absolute-offset tampering in nested containers, gaps, trailing
  bytes, huge/overflow lengths (with a tiny allocation cap), non-canonical
  padding, invalid UTF-8, and overlapping sibling arrays — each asserting the
  exact exception class.
* `test_selectors.py` — known 4-byte selectors cross-checked against
  `eth_utils.keccak` and a published `keccak256("")` digest.
* `test_integer_rules.py` — sign extension, fill rules, and range validation.
* `test_chain_kernel.py` — signing/recovery, transfers, approvals, nonce
  mismatch, bad signature, insufficient balance, truncated/unknown calldata,
  wrong chain id — each expecting a specific chain error.
* `test_replay_storage.py` — deterministic replay that preserves failures with
  error codes, persists runs/transactions/accounts, and is reproducible.
* `test_api.py` — end-to-end HTTP behavior including the 400 error category.

### Logs that correlate to inputs / run identity

* Every structured log line carries a `run_id`, `component`, `step`, and
  `verdict` (`ok` / `fail` / `error`) with the reason and relevant offset/type.
* Each pytest invocation writes `artifacts/<run-id>.jsonl` (per-test inputs,
  steps, verdicts) and `artifacts/<run-id>.summary.json` (counts + exit status).
  Override with `ABI_TEST_RUN_ID`.
* HTTP requests accept/echo an `x-run-id` header.
* Failures and unknown states are recorded as failures with a category — they
  are never reported as success.

---

## Dependency locking

* `requirements.txt` — direct dependencies with exact pins.
* `requirements.lock.txt` — full transitive lock. After `bash setup.sh` you can
  regenerate a fully resolved lock with:

  ```bash
  . .venv/bin/activate
  pip freeze --all > requirements.lock.txt
  ```

> Note on the build environment used while developing: the sandbox classifier
> blocked `pip install`, so the authoritative `pytest` suite could not be
> executed here. The dependency-free `tools/selfcheck.py` **was** executed and
> passes (see `TEST_RUN_REPORT.md`). After running `bash setup.sh`, execute
> `pytest`; any library-version mismatch would surface there.

---

## API summary

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + version |
| POST | `/abi/encode` | encode `{types, values}` → hex |
| POST | `/abi/decode` | decode `{types, data}` → values (strict categories) |
| POST | `/abi/selector` | 4-byte selector + canonical signature |
| POST | `/abi/encode-call` | selector + encoded args |
| POST | `/replay` | run the deterministic scenario |
| GET | `/runs`, `/runs/{id}` | replay history |
| GET | `/transactions` | transactions (incl. failed, with `error_code`) |
| GET | `/accounts`, `/account/{addr}` | state snapshot / one account |
| GET | `/events` | emitted events |

See `EXAMPLES.md` for copy-pasteable requests.
