# Architecture

```
┌──────────────┐   AST + spans    ┌─────────────────┐
│  ia-lang     │ ───────────────▶ │  ia-solver      │  widening/narrowing
│ lex/parse/   │                  │  abstract       │  fixpoint, branch
│ validate     │ ─────┐           │  interpreter    │  refinement, checks
└──────────────┘      │           └────────┬────────┘
       ▲              │                    │ AnalysisReport (serde)
       │              ▼                    ▼
       │     ┌──────────────────┐   ┌─────────────────┐
 source│     │  ia-concrete     │   │  ia-verify      │
       │     │  independent     │   │  differential   │
       │     │  reference exec  │──▶│  evidence       │
       │     │  (checked i64)   │   └─────────────────┘
       │     └──────────────────┘            │
       │                                     ▼
       │                            ┌─────────────────┐
       └────────────────────────────│  ia-app         │  Axum + CLI
                                    └─────────────────┘
```

The dependency direction matters: **`ia-concrete` never depends on
`ia-solver`** (or `ia-intervals`). The reference executor is a second,
independent implementation of the semantics in [SEMANTICS.md](SEMANTICS.md),
so differential tests do not grade the core using answers the core itself
produced.

## Crates

| crate | responsibility |
|---|---|
| `ia-lang` | tokens, AST (every node carries a byte/line/column `Span`), recursive-descent parser, name validation and symbol table |
| `ia-intervals` | bounded i64 interval lattice: join/meet/subset, jump-to-bound **widening**, meet-based **narrowing**, checked arithmetic images with separate overflow/div-by-zero flags |
| `ia-concrete` | concrete reference semantics: `checked_*` arithmetic, arrays, per-run step limit, input enumeration, per-site trace |
| `ia-solver` | abstract state (scalar intervals + per-cell/summary array domain), affine branch narrowing, loop fixpoints, check records with safe/possible/guaranteed/unreachable verdicts |
| `ia-verify` | exhaustively enumerates inputs, runs `ia-concrete`, and asserts the soundness contract against an `ia-solver` report |
| `ia-app` | Axum router (`/healthz`, `/api/analyze`, `/api/verify`), CLI (`serve`/`analyze`/`verify`), JSON config overlay, request-correlated logging |
| `ia-tests` | independent integration suite: differential soundness, hand-written concrete outcomes, fixpoint behaviour, HTTP end-to-end |

## Abstract domain

* Scalars: `Bottom` (empty/unreachable) or `Range { lo, hi }` over i64.
  `join` is the pointwise hull; `meet` is intersection.
* Arrays: a per-cell vector `cells[0..n]` plus a `summary` interval. A write
  at an exactly-known in-range index is a **strong update**; any other write
  weakly joins the value over the covered cells and the summary. A read at an
  index spanning multiple cells joins those cells with the summary.

## Loop fixpoints

For `while c b` with entry state `E`, the head invariant is the least fixed
point of `F(X) = E ⊔ b(X ⊓ c)`.

* **Ascending chain.** `X0 = ⊥`; the first image is kept un-widened for
  precision; thereafter `X_{n+1} = X_n ∇ F(X_n)` using jump-to-bound
  widening (an unstable endpoint jumps straight to the i64 sentinel).
  Iteration stops when `F(X_n) ⊆ X_n` — a post-fixpoint — or at
  `max_widen_iterations` (the widened bound is then accepted, still sound).
* **Descending chain (optional).** when `narrowing` is enabled, up to
  `narrowing_iterations` of `Y_{n+1} = Y_n △ F(Y_n)` (pointwise meet) sharpen
  the post-fixpoint. Narrowing cannot invalidate soundness; it only descends.
* `plain_fixpoint` skips widening entirely for tiny-range fixtures/tests.

Per-loop statistics (strategy, ascending/descending iteration counts,
whether the cutoff fired) are embedded in the report.

## Branch refinement

`assume(state, cond, truth)` cuts scalar intervals using only **affine**
constraints `(±1)·x + k ROP m`; comparisons against an interval-valued
expression are cut using that interval's bounds (e.g. `x < n` with
`n ∈ [0,5]` bounds `x ≤ 5`). Pure-constant comparisons are decided directly,
which is how contradictory branches become `Bottom` (unreachable).
Non-affine expressions (array reads, division, two-variable relations) apply
the identity refinement — always sound, just less precise.

## Failure propagation

Expression evaluation returns `(value, state)`. Arithmetic/index operations
register their check at the site and then:

* if failure is merely *possible*, the result is approximated to `TOP` and
  the **state stays reachable** (the surviving executions continue);
* if failure is *guaranteed* for every reaching input, the state becomes
  `Bottom`.

Unreachable subtrees are discovered with a separate purely syntactic walk so a
check site can never disappear from the report merely because dataflow did
not reach it. A reachable visit to a site always dominates a prior
unreachable record (and vice-versa is forbidden).

## Traceability

* Every AST node and every check carries the parser's `Span` (byte offset +
  1-based line/column).
* Responses are wrapped in an envelope carrying `request_id`, service/solver/
  language versions, ordered processing `steps`, separate `diagnostics`
  (hard failures) and per-check `explanation` text with explicit
  over-approximation wording.
* Structured single-line logs on stderr (`timestamp request_id=… stage=…`)
  correlate with the envelope id.

## What is deliberately not attempted

* relational/numeric domains beyond intervals (no octagons/polyhedra);
* interprocedural analysis (no functions in the language);
* symbolic/multi-variable narrowing beyond the affine single-variable form;
* reasoning about values past a *possible* fault more precisely than TOP.

These are precision trade-offs, not unsoundness: the verifier confirms the
weaker results still contain every concrete result.
