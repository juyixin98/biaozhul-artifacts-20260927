# Local Custom Resource Controller

A pure-backend **custom resource controller** built on Go's standard-library
HTTP server and SQLite, with a strict separation between the **desired-state
plane** and a **fault-injectable actual resource service**.

Everything runs locally with synthetic fixtures: no cloud account, no
external services. The actual plane can be told to lose responses, fail
deletes, force version conflicts or serve stale snapshots so the controller's
correctness can be observed deterministically.

## Three version concepts

They are deliberately distinct columns, never conflated:

| Concept | Column | Who changes it | Meaning |
|---|---|---|---|
| Desired generation | `generation` | desired API, only when **spec** changes | which spec version the user asked for |
| Observed generation | `status.observedGeneration` | controller, guarded | highest generation confirmed applied externally; never moves backwards or ahead of `generation` |
| Resource version | `resourceVersion` | desired API, on **every** write (incl. status/finalizers) | optimistic-concurrency token |

A no-op spec write (identical content) bumps `resourceVersion` but **not**
`generation`. Specs are compared by a canonical, key-order-independent
SHA-256 (`specHash`).

## Guaranteed behaviours

1. **Finalizer-gated deletion.** Deleting a record sets a deletion timestamp;
   the row is physically purged only after the external resource is confirmed
   gone and the controller's finalizer is removed.
2. **Claim, don't duplicate, after a lost create.** If an external create
   commits but its response is lost (500 `create-response-loss`), the
   controller queries by owner UID and adopts the committed resource. A
   duplicate physical resource is never created (the actual plane also makes
   create idempotent per owner).
3. **Conflict → requeue, never overwrite.** A status/spec write that loses
   optimistic concurrency (409) is requeued and retried against a fresh read;
   a newer spec is preserved, never clobbered.
4. **Stale observations rejected.** An external GET can deliberately return
   an older snapshot. The controller compares its actual `version` against
   the highest version it has already acted on, records
   `StaleObservation`, and refuses to regress.
5. **Duplicate events coalesce.** A dedup workqueue collapses repeated events
   for one object into a single processing round.

## Layout

```
cmd/
  apiserver/        desired-state HTTP API (SQLite)
  actualserver/     physical resource service + fault-injection admin API (SQLite)
  controller/       reconcile loop + read-only diagnostics server
internal/
  model/            resource + ledger types (failure categories, decisions)
  specutil/         canonical spec hashing
  logx/             structured JSON logging with recursive secret redaction
  store/            desired-plane persistence (optimistic concurrency, guards)
  actualstore/      actual-plane persistence, conditional update/delete, snapshots, counters, request log
  controllerstore/  controller bookkeeping: attempts, last-accepted actual version, decision ledger
  apiserver/        desired-plane HTTP handlers + one-shot test fault hook
  actualserver/     actual-plane HTTP handlers + fault injection
  desired/          controller-side HTTP adapter for the desired plane
  actual/           controller-side HTTP adapter for the actual plane (error classification)
  reconcile/        the state machine, dedup/backoff queue, diagnostics
  config/           JSON config loading/validation
configs/            example JSON configs
test/scenarios/     independent black-box end-to-end tests (HTTP only)
examples/           curl-style demo scripts
docs/               additional documentation
```

The controller talks to both planes **only over HTTP** and keeps its own
SQLite database. It never opens the other planes' databases.

## Quick start

Requires Go 1.23+. The SQLite driver is pure-Go (`modernc.org/sqlite`), so no
CGO or compiler toolchain is needed.

```bash
go build ./...

# three terminals (or background them)
./bin/apiserver      -config configs/apiserver.json     # :18081
./bin/actualserver   -config configs/actualserver.json  # :18082
./bin/controller     -config configs/controller.json    # + diagnostics :18083
```

### Example calls

```bash
# create
curl -s -X POST http://127.0.0.1:18081/api/v1/namespaces/demo/resources \
  -H 'Content-Type: application/json' \
  -d '{"name":"widget","spec":{"replicas":2,"secret":"topsecret"}}'

# read (secret is redacted without the controller credential)
curl -s http://127.0.0.1:18081/api/v1/namespaces/demo/resources/widget

# update (optimistic concurrency: quote the current resourceVersion)
curl -s -X PUT http://127.0.0.1:18081/api/v1/namespaces/demo/resources/widget \
  -H 'Content-Type: application/json' -H 'If-Match: 3' \
  -d '{"spec":{"replicas":5,"secret":"topsecret"}}'

# physical truth (independent oracle used by tests)
curl -s http://127.0.0.1:18082/admin/resources
curl -s http://127.0.0.1:18082/admin/counters

# inject faults: ownerUID is the desired object's uid
curl -s -X POST http://127.0.0.1:18082/admin/faults \
  -H 'Content-Type: application/json' \
  -d '{"ownerUID":"<uid>","fault":"delete-failed"}'
curl -s -X DELETE http://127.0.0.1:18082/admin/faults/<uid>   # heal

# controller decision ledger for one object
curl -s http://127.0.0.1:18083/diagnostics/ledger/<uid>

# delete (stays terminating until external cleanup + finalizer removal)
curl -s -X DELETE http://127.0.0.1:18081/api/v1/namespaces/demo/resources/widget
```

Run the scripted demos:

```bash
python3 examples/demo.py                 # full lifecycle incl. delete fault
./examples/demo-lost-create.sh           # deterministic lost-create -> claim
```

## Fault vocabulary (actual plane: `POST /admin/faults`)

| Fault | Effect |
|---|---|
| `create-response-loss` | create commits the row, then responds 500 `X-Fault` |
| `delete-failed` | delete responds 500 and keeps the row (retry scenario) |
| `delete-response-loss` | delete removes the row, then responds 500 |
| `update-conflict` | conditional PUT always returns 412 |
| `update-response-loss` | update commits, then responds 500 |
| `stale-get` | GET serves the last pre-update snapshot with `X-Served-Snapshot: true` and the true version in `X-Current-Version` |

Desired plane (`POST /admin/faults`, test-only): `status-conflict-once` makes
the next status write for a uid fail with 409 exactly once.

## Diagnostics: why accepted / rejected / undecidable

Every reconcile step appends a decision record with `phase`, `decision`,
`category`, both generations, the actual resource version, the detail text
and the correlation `requestID` (also present on every HTTP response via
`X-Request-ID`). Categories: `Conflict`, `ResponseLost`, `Transient`,
`StaleObservation`, `NotFound`, `Refused`.

Logs are one JSON object per line. Values under keys containing `secret`,
`token`, `password`, `credential` or `apikey` (nested, any depth) are
replaced with `***REDACTED***` before logging or unprivileged API reads.

## Tests

```bash
go test ./...                 # unit + independent black-box scenarios
go test -race ./...           # race detector
```

`test/scenarios` spins up real HTTP servers backed by three independent
SQLite files and drives them over HTTP only; expected outcomes are asserted
against the actual plane's own resource list/counters and the controller
ledger, not against the controller's internal functions. Covered scenarios:

- happy path convergence + duplicate-event coalescing + redaction,
- create interruption → claim (exactly one physical resource),
- delete retry while faulted, then purge and ownership at each phase,
- generation advance driving an external update,
- stale observation rejected then converged,
- desired-plane status conflict → requeue without overwriting new spec,
- external update conflict (412) → re-observe and converge,
- lost delete response → idempotent confirmation, no recreate.

## Remaining limitations

- Single-process, single-writer SQLite per plane (`SetMaxOpenConns(1)`);
  adequate for a local controller, not a horizontally scaled deployment.
- Controller auth is a single shared localhost secret (a fixture), not real
  authn/authz, TLS or per-user RBAC.
- Discovery is event-driven plus one periodic resync; there is no persistent
  watch/stream protocol or leader election.
- Reconcile is scoped to one opaque spec map; there is no schema validation,
  admission webhooks or generic subresource support beyond status/finalizers.
- Actual-plane semantics model an idempotent/conditional REST resource; real
  clouds differ in idempotency keys and pagination, which an adapter for them
  would need to map.
