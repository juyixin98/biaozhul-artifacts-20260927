# Algorithm contract

This is the behavioural specification the controller implements. The
independent acceptance reference (`acceptance/reference`) was written from
this document, not from the production code.

## Inputs

Per instance, the local fixture stores the **latest** load report
`(instance_id, load, reported_at)`. A reconcile tick also receives the
controller time `now` (unix seconds). Each active replica slot
`instance-001 … instance-NNN` is classified:

| Class    | Condition                                          | Load fed to aggregation |
|----------|----------------------------------------------------|-------------------------|
| fresh    | a report exists and `now − reported_at ≤ stale_skew` | the reported load       |
| stale    | a report exists and `now − reported_at > stale_skew` | imputed `T`             |
| missing  | no report ever recorded for the slot               | imputed `T`             |

`stale_skew = 30s` in the default config. **Age exactly 30s is fresh**
(inclusive boundary).

### Conservative treatment of missing instances

A non-reporting instance is assumed to carry exactly the per-replica target
load `T = 10` — never zero (which would manufacture a scale-down from silence)
and never its last reported value (stale data must not be able to drive a
scale-up).

## Aggregation and target formula

```
total_load   = Σ reported load over fresh instances
             + T × (stale + missing instances)
average_load = total_load / current_replicas
utilisation  = average_load / T
raw_desired  = ceil(total_load / T)          # HPA-style target formula
clamped      = clamp(raw_desired, min_replicas, max_replicas)
```

`ceil` uses an epsilon so a value exactly on an integer does not round up
(`total = 20, T = 10 → raw = 2`, not 3).

## Gates, in order

For a non-zero fleet the tick runs these checks in sequence and stops at the
first that applies:

1. **Fresh availability.** If `fresh_count == 0` → `noop / NO_FRESH_METRICS`.
   No scale in either direction.
2. **Fresh fraction.** If `fresh_count / current < min_fresh_fraction`
   (default `0.5`) → `noop / FRESH_FRACTION_LOW`. Too much uncertainty to
   move; missing reports are already imputed at T.
3. **Tolerance deadband.** If `utilisation ∈ [1−tol, 1+tol]` (tol = 0.10,
   edges inclusive) → `noop / WITHIN_TOLERANCE`.
4. **Direction:**
   - `clamped > current`: scale-up path.
   - `clamped < current`: scale-down path.
   - equal: floor/cap noop.

## Scale-up rate limit (per tick)

```
ceiling = max(floor(current × up_factor), current + floor(up_floor))
target  = min(clamped, ceiling, max_replicas)
```

Defaults: `up_factor = 2`, `up_floor = 1` (at least one new replica). When the
formula target is reduced by the ceiling the decision carries the
administrative reason `UP_RATE_LIMITED`. If the cap alone blocks the move the
reason is `MAX_REPLICAS_CAPPED`.

Example: 2 instances at load 30 each → total 60 → raw 6 → ceiling 4 → the
fleet moves **2 → 4** in one tick, not to 6.

## Scale-down stable window

The scale-up rate limit and the scale-down window are maintained
**independently** (separate state, separate condition). Every non-zero tick
records a durable evidence point `(now, raw_desired)` *before* the decision is
finalised.

A downscale is permitted only when:

1. the window `[now − down_window, now]` is fully covered by recorded ticks —
   the oldest stored point in range must sit at (or before) the window start,
   so a cold start or a restart onto a fresh database cannot skip the wait;
2. and the **maximum** `raw_desired` over the whole window (`window_max`) is
   below the current fleet.

The scale-down target is `min(window_max, max_replicas)`. Otherwise the tick
is `noop / WINDOW_PENDING`. A single one-tick spike therefore holds the fleet
large for the entire 60s after it, including the tick where the spike sits
exactly on the window edge; one tick later it leaves the window and the
downscale proceeds.

## Zero-replica policy (independent)

When `current == 0`, instances cannot report metrics at all, so the
per-instance path is not used. The tick reads an out-of-band **demand
signal**:

- no signal, or signal `present = false` → `noop / ZERO_NO_DEMAND`
- signal older than `stale_skew` → `noop / ZERO_DEMAND_STALE` (an expired
  signal cannot wake the fleet)
- fresh signal with `present = true` → `scale_up` to exactly
  `bootstrap_replicas` (default 1), reason `FROM_ZERO_BOOTSTRAP`

## Durability

All state lives in one local SQLite file: fleet size, latest per-instance
report, latest demand signal, evidence history and the explainable decision
log. A process restart restores all of them; the evidence window is not
re-learned after restart. The current schema is `user_version = 1` (reported
as `version=schema1` at startup).

## Failure categories

Dependency failures are decisions of action `error`, tagged with a category,
never silent 500s:

| Category                | When                                        |
|-------------------------|---------------------------------------------|
| `FLEET_READ_FAILED`     | reading the current replica count failed    |
| `METRIC_READ_FAILED`    | reading samples or the demand signal failed |
| `ADAPTER_APPLY_FAILED`  | the local fleet refused SetReplicas         |
| `STORE_FAILED`          | evidence/decision persistence failed        |
| `INVALID_INPUT`         | malformed HTTP request (400, not a tick)    |

Faults are injected only through the local admin endpoint driving the local
synthetic fixture; there are no external participants.

### Mutation ordering on failure

A scale action mutates the fixture first and persists the decision row second.
Consequently:

- `ADAPTER_APPLY_FAILED` guarantees the fleet was **not** changed.
- `STORE_FAILED` from the decision append can occur **after** a successful
  fixture mutation: the fleet did move, but the explanatory decision row is
  missing. The tick still returns the failure class so a caller never mistakes
  it for success, and the durable evidence point (written before the decision)
  keeps window accounting intact. Re-running the tick reconciles to the new
  state rather than issuing a duplicate scale.

