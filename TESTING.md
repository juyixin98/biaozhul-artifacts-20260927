# Verification and test record

How the backend is verified, what is asserted, and the recorded result of
running it from a clean directory. Every assertion below checks a **concrete
result or a concrete failure category**; no test merely checks that an
interface can be called.

## Commands

```bash
cargo test --workspace                   # unit + independent integration + HTTP
cargo clippy --workspace --all-targets -- -D warnings
./scripts/run_demo.sh                    # build, test, clippy, boot, curl examples
```

## Recorded run (this repository)

* Toolchain: `rustc 1.98.1 (48a229cea 2026-09-01)`, `cargo 1.98.1`.
* `cargo test --workspace`: **59 passed, 0 failed** (4 pn-core, 2 pn-fixtures,
  7 pn-lang, 4 pn-server, 4 pn-solver, **38 pn-tests**).
* `cargo clippy --workspace --all-targets -- -D warnings`: clean.
* `scripts/run_demo.sh`: server booted, all three standard nets returned the
  verdicts recorded below, the invalid request returned HTTP 422 `SEMANTIC`,
  and the server log correlated each via its run id.

These numbers are reproducible; if your run differs, treat it as a real
regression rather than re-recording it.

## What is independently cross-checked (and why it is independent)

The kernel BFS in `pn-core` is **not** allowed to grade its own homework:

1. **Independent semantics.** `pn-fixtures::oracle::indie::PlainNet` copies
   the net into plain `(place, weight)` tables and re-implements enabling,
   atomic consume/produce and capacity enforcement itself. It never calls a
   kernel firing function. `crosscheck.rs` asserts the kernel reachable set
   equals this independent closure for every fixture.
2. **Two more independent enumerators.** A cursor flood-fill and a fixpoint
   closure (`oracle::enumerate`, `oracle::closure_reachable`) are written
   differently from the kernel BFS (different frontier structure and no
   parent tracking) and must agree with each other and with `indie`.
3. **Independent evidence.** `pn-verify` replays witness paths firing by
   firing, recomputes `y^T C = 0` straight from the arcs for invariants, and
   attempts every transition itself to confirm a deadlock. The HTTP response
   embeds these independent verdicts; the server never ships an unverified
   witness or deadlock claim.

## Concrete assertions by area

### Atomic firing law (`firing_law.rs`, pn-core units)

* A transition consuming two input places produces exactly the expected
  successor `[0,0,1]`; being short on one input returns
  `INPUT_NOT_SATISFIED`.
* Firing into a full place returns `CAPACITY_OVERFLOW` reporting the would-be
  count `3` against capacity `2` — proving refusal, not truncation.
* A weight-3 input with only 2 tokens is `INPUT_NOT_SATISFIED`; with 3 it
  fires to `[0,1]`.
* A self-loop that would overflow a zero-capacity output place is forbidden
  (`CAPACITY_OVERFLOW`).
* Construction rejects initial-over-capacity, unknown place and zero weight,
  each matched to a specific `CoreError` variant.

### Mutex (`mutex.rs`)

* Held `[0,1]` is reachable in exactly one `acquire`; witness before/after
  markings asserted.
* `[1,1]` — inside the capacity box — is `UNREACHABLE` with no path.
* Exactly two reachable states and zero deadlocks; both states independently
  have exactly one enabled transition.
* A fabricated witness that releases while idle is rejected with category
  `FIRING_ILLEGAL` / kernel `INPUT_NOT_SATISFIED`.
* The `(1,1)` resource vector is independently a true P-invariant yet gives
  different sums on `[1,0]` vs `[1,1]`, demonstrating the condition is
  necessary, not sufficient.

### Producer/consumer (`producer_consumer.rs`)

* The reachable set equals the **hand-derived 9-marking matrix** and the
  independent closure.
* Full buffer `[0,2,0]`: `produce` is `INPUT_NOT_SATISFIED`, `consume` yields
  `[1,1,1]`.
* Targets: `[2,0,1]` reachable in produce→consume (2 steps); `[1,0,0]`
  in-box but `UNREACHABLE` (violates `free+ready=2`); `[0,2,0]` reachable in
  2 steps.
* Unique deadlock `[0,2,2]`, independently confirmed; at it `produce` fails
  `INPUT_NOT_SATISFIED` and `consume` fails `CAPACITY_OVERFLOW` (done would
  hit 3) — the two distinct refusal reasons are both asserted.
* A state-limited search returns `INCONCLUSIVE`/`STATE_LIMIT`, never a false
  `UNREACHABLE`.

### Deadlock net (`deadlock.rs`)

* `[0,0,1]` reachable in exactly `t1`,`t2` with intermediate `[0,1,0]`.
* Exactly one deadlock at distance 2, confirmed by BFS, the independent
  closure and the standalone verifier; `[0,1,0]` is correctly non-terminal
  with `t2` enabled.
* In-box `[1,0,1]` is `UNREACHABLE`; an over-capacity claim `[0,0,5]` is
  flagged `within_capacity=false`.

### Invariants (`invariants.rs`)

* Every Farkas-generated candidate passes the independent `y^T C = 0`
  recomputation for all fixtures.
* Mutex yields `(1,1)`; the weighted net yields `(1,2)` reflecting the arc
  weights, with weighted sums `[2,2]` across initial/packed markings.
* A non-invariant vector `(1,0)` is independently rejected with weighted
  change `[-1,1]`.

### HTTP end-to-end (`api.rs`, boots the real Axum router)

* `/health`, `/version` (including `unbounded_reachability_complete=false`).
* Mutex response: REACHABLE + verified witness for held, UNREACHABLE for
  `[1,1]`, run id echoed, scope disclaimer present.
* Producer/consumer response: concrete per-target verdicts and the verified
  `[0,2,2]` deadlock.
* Failure classes over HTTP: garbage → 400 `SYNTAX`; missing required fields
  → 400 `SCHEMA` with pointer `/transitions`; unknown place → 422 `SEMANTIC`
  with pointer `/transitions/0/inputs/0/place`; target over capacity → 422
  `SEMANTIC` (rejected, never clamped).
* An unsafe client run-id hint is replaced by a generated `run-…` id; a safe
  one is honoured.

## Diagnostics and correlation

Each request gets a run id (UTC micro-timestamp + pid + counter, or a
validated `X-Run-Id`). Structured `tracing` lines carry `run_id`, request
byte size, net dimensions, progress-relevant counters
(`total_states_expanded`, `deadlocks`, `invariants`, `truncated`) and the
verdict basis (`stop_reason` EXHAUSTED vs STATE_LIMIT). Rejected input logs
the category and detail count. Example correlated line from the recorded
demo:

```
WARN pn_server::http: input rejected run_id="demo-invalid-0004" category="SEMANTIC" count=1
INFO pn_server::http: analysis complete run_id="..." targets=1 total_states_expanded=5 deadlocks=0 invariants=1 truncated=false
```
