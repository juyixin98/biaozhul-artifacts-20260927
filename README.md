# Interval abstract interpreter for bounded integers

A review-ready, multi-module Rust backend that statically analyses a small
integer language (IAL) with branches, loops and array-index checks. Integers
are **bounded `i64` with checked semantics** — never silently mixed with
unbounded math integers — and loops use **widening with optional narrowing**
to compute a fixpoint.

It reports, per source site: **safe**, **possible_failure** (an explicit
over-approximation, never called a definite bug), **guaranteed_failure**, and
**unreachable**. An independent concrete reference executor and exhaustive
small-domain differential tests back the soundness claims.

## Layout

```
crates/
  ia-lang/       lexer, AST (with spans), parser, validation
  ia-intervals/  bounded i64 interval lattice (join/widen/narrow, arithmetic)
  ia-concrete/   independent concrete reference executor (checked i64)
  ia-solver/     abstract interpreter: refinement, fixpoints, checks
  ia-verify/     exhaustive concrete-vs-abstract evidence verification
  ia-app/        Axum HTTP service + `ia` CLI
  ia-tests/      independent integration tests
fixtures/        reusable .ial programs
config/          pinned default JSON configurations
scripts/verify.sh one-command reproducible check
docs/            SEMANTICS.md (normative), ARCHITECTURE.md
```

## Build / test / verify

```bash
cargo build --release
cargo test --workspace
scripts/verify.sh            # tests + clippy(-D warnings) + exhaustive verify + release
scripts/verify.sh --quick    # tests + exhaustive verify only
```

All third-party versions are pinned exactly in the workspace `Cargo.toml`
(`serde = =1.0.219`, `axum = =0.8.4`, …); `Cargo.lock` is committed.

## CLI

```bash
# human-readable
./target/release/ia analyze fixtures/01_loop_growth.ial
./target/release/ia verify  fixtures/02_branch_refine.ial

# with explicit configuration / machine-readable output / request id
./target/release/ia analyze --config config/analyze.default.json \
    --json --request-id demo-1 fixtures/03_overflow_paths.ial
./target/release/ia verify  --config config/verify.default.json \
    --cap 50000 fixtures/06_countdown.ial

./target/release/ia serve --bind 127.0.0.1:8080
```

Exit codes (`verify`): `0` sound, `3` could not run (e.g. input domain over
cap — the exhaustive check is reported **NOT run**, never passed), `4`
unsound.

## HTTP

```bash
curl -s localhost:8080/healthz
curl -s -XPOST localhost:8080/api/analyze \
  -H 'content-type: application/json' \
  -d '{"source":"input x [0:3]; { skip; }","request_id":"r1"}'
curl -s -XPOST localhost:8080/api/verify \
  -H 'content-type: application/json' \
  -d '{"source":"…","enumeration_cap":100000}'
```

Every response is an envelope:

```jsonc
{
  "request_id": "…", "service_version": "0.1.0",
  "solver_version": "0.1.0", "lang_version": "0.1.0",
  "ok": true,
  "diagnostics": [ ],            // hard failures live here, separate from findings
  "steps": [ {"stage":"parse",…}, {"stage":"analyze",…} ],
  "data": {
    "checks": [ { "kind":"index_bounds", "verdict":"possible_failure",
                  "certainty":"possible", "span":{…}, "explanation":"…over-approximation…" } ],
    "counts": { "safe":…, "possible_failure":…, "guaranteed_failure":…, "unreachable":… },
    "fixpoints": [ … ], "trace": [ … ]
  }
}
```

## Language sketch

```text
input n [0: 5];            // non-deterministic bounded input
const LEN = 4;
array a[LEN];              // zero-initialised, 1 <= len <= 2^20
{
  x = 0;
  while (x < n) { a[x] = x * 2; x = x + 1; }
  if (i >= 0 && i < LEN) { y = a[i]; }
  assert(x <= n);
}
```

Operators: `+ - * / %`, comparisons `< <= > >= == !=`, logical `! && ||`
(non-short-circuit), unary `-`. See `docs/SEMANTICS.md` for the full
specification and failure model.

## Evidence strategy

`ia-verify` enumerates the Cartesian product of the declared input ranges
(up to `enumeration_cap`), executes each with `ia-concrete`, and asserts:

* every concrete failure maps to an abstract site admitting that failure
  (same category, same source offset);
* every concrete scalar exit value is inside the abstract exit interval;
* nothing concrete is labelled unreachable;
* every `guaranteed_failure` over a fully covered domain is witnessed.

The reference answers (hand-written concrete outcomes + the independent
executor) live in `crates/ia-tests/tests/`, not in the solver. When the
domain is too large to enumerate, the check is reported as **not
cross-validated / not run** rather than claimed passing.
