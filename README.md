# ROBDD Boolean-Function Backend

A reduced ordered binary decision diagram (ROBDD) backend for Boolean
functions, written in Rust with **Axum** and **Serde**. It supports building
functions from a small Boolean language, applying binary connectives,
restricting variables, garbage collection while preserving live roots, and
equivalence queries backed by an **independent** truth-table oracle.

Everything runs locally with synthetic data — no external services or
accounts.

---

## 1. Quick start

Requires Rust 1.98+ (stable).

```bash
# run the whole evidence suite
cargo test

# lints
cargo clippy --all-targets

# library demonstration (no server needed)
cargo run --example kernel_demo

# start the HTTP backend with the bundled local config
cargo run -- --config config/dev.toml
# then in another shell:
curl -s localhost:8080/healthz
curl -s localhost:8080/equiv \
  -H 'content-type: application/json' \
  --data @data/equiv_accepted.json
```

### Configuration

Resolution order: built-in defaults → TOML file → environment variables.

| Key / env var | Default | Meaning |
|---|---|---|
| `bind_addr` / `BDD_BIND_ADDR` | `127.0.0.1:8080` | listen address |
| `max_truth_table_rows` / `BDD_MAX_TRUTH_TABLE_ROWS` | `65536` | exhaustive-verification budget; larger tables make a query `inconclusive` rather than guessed |
| `log_level` / `BDD_LOG` | `info` | `tracing` filter |

---

## 2. The Boolean input language

```text
expr   := equiv
equiv  := implies ("<->" implies)*       # equivalence
implies:= xor     ("->" xor)*            # implication (right-assoc)
xor    := or      ("^" or)*
or     := and     ("||" and)*
and    := unary   ("&&" unary)*
unary  := "!" unary | atom
atom   := "true" | "false" | ident | "(" expr ")"
ident  := [A-Za-z_][A-Za-z0-9_]*
```

Precedence (tightest first): `!`, `&&`, `||`, `^`, `->`, `<->`.
Examples: `a -> b`, `!(a && b)`, `(x || y) && !z`.

---

## 3. Architecture — modules with real responsibilities

```
src/
├── lang/        input language: ast.rs, lexer.rs (spans), parser.rs
├── kernel/      the solver
│   ├── edge.rs     complement-pointer edge encoding (parity bit)
│   ├── manager.rs  fixed order, unique table, builder, apply, restrict, eval
│   ├── gc.rs       mark-sweep-compact GC with stable logical node ids
│   └── error.rs    typed, categorized kernel errors
├── oracle.rs    INDEPENDENT truth-table interpreter (never imports the kernel)
├── verify.rs    equivalence evidence: structural isomorphism + oracle
├── api/         Axum surface: dto.rs, state.rs, handlers.rs
├── diag.rs      correlation ids, accept/reject/inconclusive reasons, redaction
├── config.rs    layered configuration
└── main.rs      server binary
tests/           integration tests (exhaustive cross-checks + HTTP black box)
examples/        runnable library demo
data/            sample request fixtures
config/          local startup configuration
```

### Kernel invariants

* **Fixed variable order.** Variables are positions (`VarId`) in the order
  given at manager creation; children always test later variables.
* **Unique table by `(var, low, high)` with redundant-node elimination.**
  `mk` first collapses redundant tests (`low == high` ⇒ no node), then interns
  the triple, so each function has exactly one canonical encoding and
  isomorphic subfunctions share a node.
* **Uniform complement handling.** One parity bit per edge: negation is a free
  bit flip, there is a single terminal, and the unique table canonicalizes a
  complemented low edge onto the node (low is always uncomplemented).
* **No cross-manager mixing.** Every `NodeRef` carries its manager id and a
  stable logical edge; use on another manager gives `foreign-manager` — never a
  silent misread.
* **GC preserves every reference that is still semantically live.** Edges name
  stable logical ids, not arena slots. On collection the arena is compacted but
  surviving nodes keep their id via a redirect table, so an outstanding
  reference to a node still reachable from the declared roots keeps working
  transparently; only a reference to an actually reclaimed id is rejected with
  `stale-reference`. The collecting-pass counter (`epoch`) advances whenever
  any node is reclaimed, and the roots are also returned repacked.

### Equivalence is bound to variable identity

`/equiv` takes each side's order plus an explicit `mapping` (left name → right
name), validates that it is a **bijection over the supports** and that it
**preserves the fixed order**, then compares the canonical ROBDDs under the
numeric `VarId` mapping. Identity-on-equal-names is used when no mapping is
given. Non-bijective mappings yield `non-bijective-mapping`; order-reversing
renamings yield `order-mismatch`.

---

## 4. Evidence — why the answers are trustworthy

The reference answers are **not** produced by the code under test:

* `src/oracle.rs` is a standalone AST interpreter with a plain exhaustive
  assignment enumerator. It never constructs or reads a BDD.
* Every kernel operation is checked against it: each formula, every binary
  connective, every `(variable, value)` restriction over all 2^n rows.
* Equivalence combines two independent signals — complement-edge-aware
  structural isomorphism *and* the exhaustive table. A disagreement between
  them is reported `inconclusive` (suspected kernel defect), never papered
  over; over-budget tables are also `inconclusive`, not guessed.

Tests assert concrete values and failure categories, not "the API responded":

* exact canonical reachable node counts (e.g. `a -> b` = 2, `(a&&b)||c` = 3);
* different syntax of the same function interns to the *identical* edge;
* variable renaming equivalence and rejection of order-reversing mappings;
* all 16 Boolean functions of two variables, combined with every connective
  over every row (5120 points);
* a seeded 400-step randomized build/apply/restrict/GC sequence where every
  live function is checked against an independent oracle expression after each
  step, including post-GC survival and reclamation;
* rebuild after GC preserves every truth value; reclaimed refs are
  `stale-reference`, while refs to still-shared nodes survive transparently;
* cross-manager refs are `foreign-manager`;
* rejection carries a concrete disagreeing witness.

### Real test results

```
$ cargo test
running 43 tests  (src unit tests: lang, kernel, verify, diag, config)
test result: ok. 43 passed; 0 failed
running 12 tests  (tests/api.rs, HTTP black box over the real Axum router)
test result: ok. 12 passed; 0 failed
running 9 tests   (tests/exhaustive.rs: oracle cross-checks, all-16-functions,
                   randomized GC sequence)
test result: ok. 9 passed; 0 failed
```

`cargo clippy --all-targets` reports no warnings.

### Real demo output

```
$ cargo run --example kernel_demo
a -> b  and  !a || b  intern to the same ref: true
reachable internal nodes for a -> b: 2
XOR mismatches vs exhaustive oracle over 8 rows: 0
xor | c=false at a=1,b=0 => false
gc: 8 nodes collected (11 -> 3), epoch 0 -> 1
rebuilt function after gc, reachable nodes: 3
using a pre-gc reference is rejected: true
```

---

## 5. HTTP API

All responses carry a `diag` object: `request_id`, `outcome`
(`accepted` / `rejected` / `error` / `inconclusive`), a stable `code`, a
human `reason`, non-sensitive `state`, and redacted `sensitive` fields.

| Method | Path | Body | Success |
|---|---|---|---|
| GET | `/healthz` | — | `200` |
| POST | `/managers` | `{"order":["a","b"]}` | `201` |
| GET | `/managers/:id` | — | node/epoch info |
| POST | `/managers/:id/build` | `{"expr":"a && b"}` | opaque `ref` |
| POST | `/managers/:id/not` | `{"ref":{…}}` | complemented `ref` |
| POST | `/managers/:id/apply` | `{"op":"or","lhs":{…},"rhs":{…}}` | `ref` |
| POST | `/managers/:id/restrict` | `{"ref":{…},"var":"a","value":true}` | `ref` |
| POST | `/managers/:id/sat` | `{"ref":{…}}` | witness or unsatisfiable |
| POST | `/managers/:id/gc` | `{"roots":[{…}]}` | report + repacked roots |
| POST | `/equiv` | two sides + optional `mapping` | `200` accepted/inconclusive, `422` rejected |

`op` accepts `and`/`or`/`xor`/`implies`/`equiv` (also `&&`,`||`,`^`,`->`,`<->`).

### Status codes & error categories

`400 invalid-expr` / `unknown-op` · `404 unknown-manager` ·
`422 unknown-variable`, `foreign-manager`, `stale-reference`,
`invalid-node`, `non-bijective-mapping`, `unmapped-variable`,
`order-mismatch`; semantic rejection of inequivalent functions is also `422`
with `decision: rejected` and a witness.

### Worked equivalence response (abridged)

```jsonc
// POST /equiv  {"lhs":{"expr":"a -> b","order":["a","b"]},
//              "rhs":{"expr":"!a || b","order":["a","b"]},
//              "client_label":"example-sensitive-label"}
{
  "decision": "accepted", "equivalent": true,
  "structural_equivalent": true, "oracle_checked": true,
  "assignments_checked": 4, "witness": null,
  "lhs_internal_nodes": 2, "rhs_internal_nodes": 2,
  "reason": "accepted: canonical ROBDDs isomorphic (2/2 internal nodes) and all 4 truth-table rows agree",
  "diag": {
    "request_id": "req-000000000001", "outcome": "accepted", "code": "equivalent",
    "state": {"assignments_checked": 4, "oracle_checked": true, "limit": 65536, "...": "..."},
    "sensitive": { "client_label": "redacted(23)" }
  }
}
```

The sensitive label never appears in the body or logs — only `redacted(23)`.

Inequivalence example (`a && b` vs `a || b`) returns `422`:

```json
{"decision":"rejected","equivalent":false,"witness":{"a":false,"b":true},
 "reason":"rejected: ROBDDs non-isomorphic and the independent oracle found a disagreeing assignment among 4 rows", ...}
```

---

## 6. Diagnostics

Each request gets a unique `req-…` correlation id, echoed in the response and
emitted in structured `tracing` logs, along with the decision and key state
(node counts, rows checked, epochs). The reason states *why*:

* **accepted** — structures isomorphic *and* every enumerated row agreed;
* **rejected** — structures differ and the oracle supplies a witness;
* **inconclusive** — over the enumeration budget, or the two independent
  signals contradict each other (treated as a possible kernel defect).

Sensitive inputs (client labels) pass through [`redact`](src/diag.rs), which
exposes only a character count (`redacted(n)`); Boolean formulas and variable
names in the synthetic fixtures are non-secret and shown verbatim.
