# replicactl — local replica-count controller

A local, multi-module Go implementation of a replica-count controller whose
input is per-instance load samples. It scales a **synthetic local fixture**
only; there are no cloud accounts or real workloads. State is one local
SQLite file (pure-Go driver, no CGO), the API is Go standard-library HTTP.

The acceptance bar is verifiable behaviour: load steps, missing reports,
late/delayed reports, short spikes and service restarts are compared against
both hand-computed expectations and an **independent** reference
implementation, and every decision not to act carries an explicit reason.

## Modules

```
core/                  zero external dependencies; the decision mechanism
  model/               resource model, samples, config, input validation
  controller/          reconcile loop, aggregation, gates, window, rate limit
  adapter/             in-memory synthetic fleet fixture (+ fault hooks)
app/                   standard-library HTTP service + SQLite persistence
  store/               schema/migration, decision log, evidence history,
                       durable fixture (survives process restart)
  server/              HTTP API, request-id correlation, structured logging
  config/              JSON config loader
  cmd/replicactl/      service binary
acceptance/            independent verification
  reference/           independent re-statement of the policy (imports
                       neither core nor app); hand-arithmetic unit tests
  runner/              builds + spawns the real binary, replays fixtures
                       over HTTP, performs real process restarts, injects
                       faults, and compares actual vs hand vs reference
  scenarios/           five hand-authored JSON fixtures
  cmd/acceptance/      report generator (JSON + Markdown)
config/controller.json checked-in configuration
scripts/demo.sh        live normal/abnormal/restart walkthrough
results/               generated acceptance + live-run artefacts
docs/ALGORITHM.md      the behavioural contract
```

## The policy in one paragraph

Non-fresh instances (no report, or a report older than 30s) are conservatively
imputed at target load `T=10`; the raw target is `ceil(total_load/T)`; a
per-tick scale-up is capped at 2× current (with a +1 floor); scale-downs
require the raw target to have stayed below the current fleet for a full 60s
window and move only to the window maximum; a zero fleet ignores metrics and
wakes solely from a fresh out-of-band demand signal to a bootstrap count of 1.
Full rules, gates and boundary semantics: [`docs/ALGORITHM.md`](docs/ALGORITHM.md).

## Build and test (offline)

The build needs only Go 1.23+; SQLite is the pure-Go `modernc.org/sqlite`
(v1.36.1, pinned in `app/go.sum`), so `CGO_ENABLED=0` works.

```bash
# unit + component tests (three independent modules)
(cd core       && go test ./...)
(cd app        && go test ./...)
(cd acceptance && go test ./...)          # builds the real binary; E2E over HTTP

# full workspace vet
go vet ./core/... ./app/... ./acceptance/...

# human/ machine-readable acceptance reports
go run ./acceptance/cmd/acceptance \
  -repo . -scenarios acceptance/scenarios \
  -out-json results/acceptance-report.json \
  -out-md   results/acceptance-report.md

# live normal + injected-fault + real-restart walkthrough
./scripts/demo.sh
```

`go run`/`go test` honour `GOPROXY=off` once the module cache is populated; the
pinned dependency set is fully self-contained.

## Run the service

```bash
go build -o /tmp/replicactl ./app/cmd/replicactl
/tmp/replicactl -config config/controller.json
# listens on 127.0.0.1:8080, state at ./data/controller.db
```

### HTTP surface

All responses carry the request id; send `X-Request-ID` to correlate, or one
is generated and returned.

| Method | Path                       | Purpose                                            |
|--------|----------------------------|----------------------------------------------------|
| POST   | `/v1/metrics`              | submit `{instance_id, load, reported_at}`          |
| POST   | `/v1/demand`               | zero-fleet demand signal `{present, reported_at}`  |
| POST   | `/v1/reconcile`            | run one tick; body may pin `"at"` for deterministic runs |
| GET    | `/v1/decisions?limit=n`    | recent explainable decision rows                   |
| GET    | `/v1/requests/{id}`        | look a decision up by its correlated request id    |
| GET    | `/v1/fixture`              | current fleet size, per-instance state, demand     |
| POST   | `/v1/admin/seed`           | fixture initialisation (`{"replicas": n}`)         |
| POST   | `/v1/admin/faults`         | inject/clear one synthetic dependency fault        |
| GET    | `/healthz`                 | liveness                                           |

A reconcile response contains the full observation (per-instance
fresh/stale/missing classification, loads actually used, totals, utilisation,
raw and clamped targets), the action, the resulting replica count, and the
reason list. Example excerpt:

```json
{
  "request_id": "loadstep-1",
  "action": "scale_up",
  "current_replicas": 2,
  "desired_replicas": 4,
  "reasons": ["UP_RATE_LIMITED"],
  "observation": {
    "raw_desired": 6, "fresh_count": 2, "total_load": 60,
    "utilisation_ratio": 3,
    "samples": [
      {"instance_id": "instance-001", "status": "fresh", "load": 30, "used_load": 30}
    ]
  }
}
```

Failures return HTTP 409 (or 400 for bad input) with a body of
`{error, request_id, category}` where `category` is one of the failure
classes in `docs/ALGORITHM.md`.

### Example calls

```bash
curl -sS -XPOST localhost:8080/v1/demand -H 'Content-Type: application/json' \
  -d '{"present": true, "reported_at": 1700000000}'
curl -sS -XPOST localhost:8080/v1/reconcile -H 'Content-Type: application/json' \
  -H 'X-Request-ID: boot-1' -d '{"at": 1700000000}'
curl -sS localhost:8080/v1/requests/boot-1
```

`scripts/demo.sh` is a complete copy-pasteable transcript.

## Explainability

- **Request identity:** every log line and persisted row carries the
  `X-Request-ID`; `GET /v1/requests/{id}` retrieves the decision.
- **Key steps / location:** logs include `step=reconcile_start
  location=controller.Reconcile`, `step=reconcile_done action=… reasons=[…]`,
  and startup prints `version=schema1` plus the full effective config.
- **Failures and uncertainty are separate:** dependency faults are tagged
  `class=… detail=…`; deliberate non-actions are normal 200s with a reason
  (`NO_FRESH_METRICS`, `FRESH_FRACTION_LOW`, `WINDOW_PENDING`, …) and the
  observation shows exactly which inputs were imputed.

## Reproducibility / lockfile

- Pinned dependency tree: `app/go.mod` + `app/go.sum` (`modernc.org/sqlite
  v1.36.1` and its transitive set). `core` and `acceptance/reference` have no
  third-party dependencies.
- Deterministic scenarios use explicit synthetic tick times (`"at"`), never
  wall clock; the same fixture always produces the same decision sequence.
- Artefacts from the latest verified run are committed under `results/`.
