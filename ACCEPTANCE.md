# 验收记录 (acceptance record)

Run date (UTC): **2026-09-27T13:56Z**
Toolchain: **cargo/rustc 1.98.1 (stable)**, Python **3.12.3**, Linux x86_64.

## Reproduction commands (from a clean checkout)

```bash
python3 tools/gen_golden.py     # independent Python reference -> fixtures/
cargo build --release
cargo test --release
```

## Observed results (truthful)

* Golden generator: `wrote 7 roundtrip + 4 negative golden vectors`
  (empty, single, one-symbol run, all 256 bytes, extreme skew, seeded random,
  text; negative: oversubscribed tree / truncated codeword / bad padding /
  unknown version). All valid vectors first passed the Python implementation's
  **own** decoder self-check.
* Release build: `Finished release profile` with **zero warnings**; lto=thin.
* Tests: **68 passed, 0 failed** across unit and independent integration
  suites (huff-core 29, huff-store 4, huff-api config 2, huff-it inline 2,
  golden cross-validation 3, property roundtrip 5, malformed-input 12,
  tie-break/determinism 4, in-process HTTP 7).
* CLI smoke (`/tmp/huff-accept`, fresh directory):
  * `all.bin` (all byte values ×40) roundtrip OK
  * `skew.bin` (80 000 zero bytes + one of every value) roundtrip OK
  * `rand.bin` (60 000 seeded-random bytes) roundtrip OK
  * `validate unknown_version.hfc` → overall **FAIL**, exit code **2**
  * `validate oversubscribed_tree.hfc` → overall **FAIL**, exit code **2**
* HTTP smoke (`127.0.0.1:18080`): `/healthz` 200; encode/decode 200 with
  byte-identical roundtrip; `/v1/validate` all-pass JSON; artifact listed and
  re-fetched by content id.
* Run correlation: every CLI/API log line carries a 16-hex run/request id;
  with `HUFF_LOG_DIR` set, per-run files record version, step, verdict and
  judgment basis (example in `demo/logs/run-*.log`).

No network service other than crates.io dependency download is used; all test
data is locally synthesised.
