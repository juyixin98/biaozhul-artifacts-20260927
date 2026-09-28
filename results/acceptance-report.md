# Acceptance report

Every reconcile tick is checked against (a) a hand-computed expectation embedded in the fixture and (b) an independent reference simulator in `acceptance/reference` that never imports the production code.

## late_reports — PASS

Reports delayed relative to the tick. A report aged 31s (>30s skew) is stale, excluded from aggregation and never triggers scale-up: a load-40 storm arrives as NO_FRESH_METRICS. Reports aged 20s are still fresh and drive a rate-limited scale-up. A report aged exactly 30s sits on the boundary and is treated as fresh (utilisation exactly at target -> tolerance noop).

| Tick | Request ID | Restart | Actual action | Actual desired | Reference | Hand expected | Mismatches |
|---:|---|:--:|---|---:|---|---|---|
| 1000 | `late-stale` |  | noop | 4 | noop/4 | noop/4 | — |
| 1020 | `late-fresh-up` |  | scale_up | 8 | scale_up/8 | scale_up/8 | — |
| 1030 | `late-boundary-tol` |  | noop | 8 | noop/8 | noop/8 | — |

**Why no action was taken**

- @1000 — `NO_FRESH_METRICS`: no instance reported within the stale skew; stale data cannot trigger scale-up
- @1030 — `WITHIN_TOLERANCE`: average utilisation inside the +/-10% deadband

## load_step — PASS

Abrupt load step: raw target exceeds the 2x per-tick ceiling every tick (2->4->8->16); asserts the rate-limit reason and hand-computed aggregates, the MaxReplicas=16 cap stop, and a return-to-target WITHIN_TOLERANCE noop.

| Tick | Request ID | Restart | Actual action | Actual desired | Reference | Hand expected | Mismatches |
|---:|---|:--:|---|---:|---|---|---|
| 1000 | `loadstep-1` |  | scale_up | 4 | scale_up/4 | scale_up/4 | — |
| 1010 | `loadstep-2` |  | scale_up | 8 | scale_up/8 | scale_up/8 | — |
| 1020 | `loadstep-3` |  | scale_up | 16 | scale_up/16 | scale_up/16 | — |
| 1030 | `loadstep-4-cap` |  | noop | 16 | noop/16 | noop/16 | — |
| 1040 | `loadstep-5-target` |  | noop | 16 | noop/16 | noop/16 | — |

**Why no action was taken**

- @1030 — `MAX_REPLICAS_CAPPED`: computed target above the configured maximum; fleet already at the cap
- @1040 — `WITHIN_TOLERANCE`: average utilisation inside the +/-10% deadband

## missing_reports — PASS

Two of four instances never report. Conservative imputation assigns each missing instance target load T=10; raw target is 3. Every tick before the 60s window is covered is a WINDOW_PENDING noop; then scale-down lands at the window max (3). A later fully-idle 3-instance window scales all the way to zero, proving the hysteresis window rather than immediate reaction.

| Tick | Request ID | Restart | Actual action | Actual desired | Reference | Hand expected | Mismatches |
|---:|---|:--:|---|---:|---|---|---|
| 1000 | `missing-pending-1000` |  | noop | 4 | noop/4 | noop/4 | — |
| 1010 | `missing-pending-1010` |  | noop | 4 | noop/4 | noop/4 | — |
| 1020 | `missing-pending-1020` |  | noop | 4 | noop/4 | noop/4 | — |
| 1030 | `missing-pending-1030` |  | noop | 4 | noop/4 | noop/4 | — |
| 1040 | `missing-pending-1040` |  | noop | 4 | noop/4 | noop/4 | — |
| 1050 | `missing-pending-1050` |  | noop | 4 | noop/4 | noop/4 | — |
| 1060 | `missing-down-3` |  | scale_down | 3 | scale_down/3 | scale_down/3 | — |
| 1070 | `missing-idle-pending-1070` |  | noop | 3 | noop/3 | noop/3 | — |
| 1080 | `missing-idle-pending-1080` |  | noop | 3 | noop/3 | noop/3 | — |
| 1090 | `missing-idle-pending-1090` |  | noop | 3 | noop/3 | noop/3 | — |
| 1100 | `missing-idle-pending-1100` |  | noop | 3 | noop/3 | noop/3 | — |
| 1110 | `missing-idle-pending-1110` |  | noop | 3 | noop/3 | noop/3 | — |
| 1120 | `missing-idle-pending-1120` |  | noop | 3 | noop/3 | noop/3 | — |
| 1130 | `missing-down-zero` |  | scale_down | 0 | scale_down/0 | scale_down/0 | — |

**Why no action was taken**

- @1000 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1010 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1020 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1030 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1040 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1050 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1070 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1080 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1090 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1100 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1110 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1120 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)

## service_restart — PASS

The service is killed and restarted on the same SQLite file mid-window: the scale-down evidence history survives, so the downscale fires at exactly the tick the contract predicts instead of restarting the 60s wait. Then the fleet scales to zero; three more restarts verify the independent zero policy: no signal (ZERO_NO_DEMAND), stale signal (ZERO_DEMAND_STALE), fresh signal (FROM_ZERO_BOOTSTRAP to 1).

| Tick | Request ID | Restart | Actual action | Actual desired | Reference | Hand expected | Mismatches |
|---:|---|:--:|---|---:|---|---|---|
| 1000 | `restart-pre-1000` |  | noop | 4 | noop/4 | noop/4 | — |
| 1010 | `restart-pre-1010` |  | noop | 4 | noop/4 | noop/4 | — |
| 1020 | `restart-pre-1020` |  | noop | 4 | noop/4 | noop/4 | — |
| 1030 | `restart-pre-1030` |  | noop | 4 | noop/4 | noop/4 | — |
| 1040 | `restart-pre-1040` |  | noop | 4 | noop/4 | noop/4 | — |
| 1050 | `restart-pre-1050` |  | noop | 4 | noop/4 | noop/4 | — |
| 1050 | `restart-at-1050` | yes | noop | 4 | noop/4 | noop/4 | — |
| 1060 | `restart-down-3` |  | scale_down | 3 | scale_down/3 | scale_down/3 | — |
| 1070 | `restart-idle-1070` |  | noop | 3 | noop/3 | noop/3 | — |
| 1080 | `restart-idle-1080` |  | noop | 3 | noop/3 | noop/3 | — |
| 1090 | `restart-idle-1090` |  | noop | 3 | noop/3 | noop/3 | — |
| 1100 | `restart-idle-1100` |  | noop | 3 | noop/3 | noop/3 | — |
| 1110 | `restart-idle-1110` |  | noop | 3 | noop/3 | noop/3 | — |
| 1120 | `restart-idle-1120` |  | noop | 3 | noop/3 | noop/3 | — |
| 1130 | `restart-to-zero` |  | scale_down | 0 | scale_down/0 | scale_down/0 | — |
| 1140 | `restart-at-zero-no-demand` | yes | noop | 0 | noop/0 | noop/0 | — |
| 1150 | `restart-at-zero-stale-demand` | yes | noop | 0 | noop/0 | noop/0 | — |
| 1160 | `restart-at-zero-fresh-demand` | yes | scale_up | 1 | scale_up/1 | scale_up/1 | — |

**Why no action was taken**

- @1000 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1010 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1020 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1030 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1040 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1050 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1050 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1070 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1080 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1090 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1100 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1110 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1120 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1140 — `ZERO_NO_DEMAND`: fleet at zero and no fresh out-of-band demand signal exists
- @1150 — `ZERO_DEMAND_STALE`: fleet at zero but the only demand signal is older than the stale skew

## short_spike — PASS

A single one-tick load spike (one evidence point at raw desired 16 inside a sea of zeros). Scale-up fires on the spike tick; afterwards every downscale is withheld while that point remains within the 60s stable window, including the tick where it sits exactly on the window edge. One tick later the spike leaves the window and the fleet scales all the way down to zero.

| Tick | Request ID | Restart | Actual action | Actual desired | Reference | Hand expected | Mismatches |
|---:|---|:--:|---|---:|---|---|---|
| 1000 | `spike-idle-1000` |  | noop | 8 | noop/8 | noop/8 | — |
| 1010 | `spike-idle-1010` |  | noop | 8 | noop/8 | noop/8 | — |
| 1020 | `spike-idle-1020` |  | noop | 8 | noop/8 | noop/8 | — |
| 1030 | `spike-idle-1030` |  | noop | 8 | noop/8 | noop/8 | — |
| 1040 | `spike-idle-1040` |  | noop | 8 | noop/8 | noop/8 | — |
| 1050 | `spike-idle-1050` |  | noop | 8 | noop/8 | noop/8 | — |
| 1060 | `spike-up` |  | scale_up | 16 | scale_up/16 | scale_up/16 | — |
| 1070 | `spike-held-1070` |  | noop | 16 | noop/16 | noop/16 | — |
| 1080 | `spike-held-1080` |  | noop | 16 | noop/16 | noop/16 | — |
| 1090 | `spike-held-1090` |  | noop | 16 | noop/16 | noop/16 | — |
| 1100 | `spike-held-1100` |  | noop | 16 | noop/16 | noop/16 | — |
| 1110 | `spike-held-1110` |  | noop | 16 | noop/16 | noop/16 | — |
| 1120 | `spike-held-1120` |  | noop | 16 | noop/16 | noop/16 | — |
| 1130 | `spike-down-zero` |  | scale_down | 0 | scale_down/0 | scale_down/0 | — |

**Why no action was taken**

- @1000 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1010 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1020 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1030 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1040 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1050 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1070 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1080 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1090 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1100 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1110 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)
- @1120 — `WINDOW_PENDING`: scale-down level not observed for the full 60s stable window (or a higher point remains in it)

## Failure categories (injected through the real binary)

Each case injects one synthetic dependency fault into the local fixture and asserts the concrete category and HTTP 409 of the following reconcile — not merely that an endpoint responds.

| Fault hook | HTTP | Expected category | Result | Detail |
|---|---:|---|:--:|---|
| `metric_read` | 409 | `METRIC_READ_FAILED` | PASS | — |
| `current_read` | 409 | `FLEET_READ_FAILED` | PASS | — |
| `set_replicas` | 409 | `ADAPTER_APPLY_FAILED` | PASS | — |
| `decision_append` | 409 | `STORE_FAILED` | PASS | — |
| `history_read` | 409 | `STORE_FAILED` | PASS | — |
| `demand_read` | 409 | `METRIC_READ_FAILED` | PASS | — |

