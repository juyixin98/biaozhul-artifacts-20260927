# Local Infrastructure Planner

A local, declarative infrastructure planner with a reconciliation loop. It
compares a **declared specification** against an **observation of the
(simulated) real world** and emits an ordered, dependency-aware plan of
creates / in-place updates / replaces / deletes — then applies it with
drift protection, durable journaling and outcome-driven recovery.

Everything runs locally: a Go standard-library HTTP server, a SQLite journal
and an in-process simulated resource provider. No cloud, no network calls
other than the listener.

## What it demonstrates

- **Stable resource identity.** A resource has a logical identity
  (`kind` + declared `name`) *and* a provider-assigned physical `id`. An
  in-place update keeps the physical id; a **replace** destroys the old
  physical resource and creates a new one with a new id.
- **Immutable vs mutable fields.** Changing a mutable attribute is an
  in-place `update`. Changing an immutable attribute (or a resource
  reference) is a `replace`. Replacing a resource **cascades**: every
  existing dependent is replaced too, because it physically referenced the
  resource being destroyed.
- **Dependency-aware ordering.** Creates proceed in dependency order
  (bucket/vpc → subnet → instance); destroys run in the exact reverse order.
  A replace is split into a destroy wave followed by a create wave.
- **Critical-resource guard.** Destroying or replacing a `protected`
  resource (or one protected in the real world) is refused at planning time
  until the request explicitly releases the guard for that key.
- **Plan bound to an observation.** Each plan stores the full baseline
  observation and a SHA-256 fingerprint. Before applying, the world is
  re-observed; any unrelated change is drift and the apply is **rejected**.
- **Recovery from the real outcome, not assumptions.** Every operation is
  journaled (`pending → inflight → succeeded/failed`). On retry/resume the
  loop re-observes and decides:
  - a create whose success response was lost but which really committed is
    **adopted** (no duplicate create);
  - a delete whose target is already gone is a no-op success;
  - a delete carrying a stale id where the logical name was recreated with a
    new id is refused (`id_reassigned`) rather than deleting the new
    resource;
  - an update/replace whose target vanished mid-run is a state conflict, not
    a silent recreate.
- **Distinguishable failure categories.** `input_error`, `state_conflict`,
  `resource_exhaustion`, `compute_failure` — carried on every error across
  package and HTTP boundaries.
- **Replayable operation evidence.** Each attempt records `observe`,
  `request`, `response`, `error` and `decision` evidence rows in SQLite,
  plus a per-run JSONL log with monotonic run sequence numbers.

## Supported resource model

| kind     | attrs | immutable attrs | references |
|----------|-------|-----------------|------------|
| `bucket` | `storage_class`, `versioning` | `storage_class` | — |
| `vpc`    | `cidr`, `region`, `tags` | `cidr`, `region` | — |
| `subnet` | `cidr`, `zone`, `tags` | `cidr` | `network_ref → vpc` (immutable) |
| `instance` | `image`, `shape`, `state` | `image` | `subnet_ref → subnet` (immutable) |

Specs are validated up front: required fields, unknown fields, reference
kind/arity, dangling references, duplicate names, self-references. All
problems in a document are reported in one pass.

## Layout

```
cmd/server/             HTTP entrypoint
internal/model/         shared types + typed error contract (failure categories)
internal/spec/          schema + declaration validation (input_error)
internal/planner/       pure diff/order engine; observation fingerprint
internal/provider/      Provider adapter contract + in-process simulation + faults
internal/journal/       SQLite persistence: runs, op journal, evidence
internal/reconciler/    coordination loop: plan, drift gate, apply, resume
internal/httpapi/       standard-library HTTP handlers
internal/logging/       per-run JSONL logger with run sequence numbers
examples/               sample requests + an end-to-end demo script
```

The package boundaries are deliberate: the planner is pure (no I/O) and
fully testable in isolation; the reconciler depends on a `Provider`
interface, so the simulated adapter can be swapped for a real one; the
journal is the only durable component and is the recovery source of truth.

## Run it

Requires Go 1.23+. The only third-party dependency is the pure-Go
`modernc.org/sqlite` driver (no CGO toolchain needed at build beyond a normal
Go install); versions are pinned in `go.mod` and hashed in `go.sum`.

```bash
go mod verify
go build ./...
go run ./cmd/server --addr 127.0.0.1:8080
```

Then in another shell:

```bash
# plan a stack against an empty world
curl -s localhost:8080/v1/plans -d @examples/create.json | jq

# apply it (use the run_id from the plan)
RID=...
curl -s -X POST localhost:8080/v1/runs/$RID/apply | jq

# inspect the real (simulated) world
curl -s localhost:8080/v1/live | jq

# replay what happened from persisted evidence
curl -s localhost:8080/v1/runs/$RID/evidence | jq
```

A self-contained demo (reset → seed → guard block → release → drift reject
→ lost-response recovery) is provided:

```bash
./examples/demo.sh
```

### HTTP surface

| method & path | purpose |
|---|---|
| `POST /v1/plans` | validate + observe + build + persist a plan |
| `POST /v1/runs/{id}/apply` | apply a planned run |
| `POST /v1/runs/{id}/resume` | resume an interrupted/failed run from real state |
| `GET  /v1/runs/{id}` | run record |
| `GET  /v1/runs` | recent runs |
| `GET  /v1/runs/{id}/evidence` | per-attempt evidence rows |
| `GET  /v1/live` | current simulated observation |
| `POST /v1/admin/seed` | seed live resources |
| `POST /v1/admin/faults` / `GET` | arm / list injected faults |
| `POST /v1/admin/reset` | empty the simulated world and clear faults |

Errors are returned as `{"error":{"category","code","message"}}` with HTTP
status mapped from the category: `input_error`→400, `state_conflict`→409,
`resource_exhaustion`→507, `compute_failure`→502.

### Injectable faults

`create_exhausted`, `create_commit_response_lost`, `create_unknown`,
`create_hang`, `update_transient`, `update_hang`, `delete_transient`,
`delete_hang`, `delete_not_found`. Each arms N matching operations
(`remaining`) and is then consumed, so recovery scenarios are deterministic.

## Tests

```bash
go test ./... -count=1
```

Tests assert concrete outcomes and specific failure categories, not merely
that interfaces are callable:

- dependency create order and reverse destroy order;
- mutable update preserves the physical id, immutable change replaces the
  whole chain with new ids and no duplicates;
- protected delete blocked then allowed after explicit release;
- drift before apply rejected and the drifted world left untouched;
- create success-response lost → adopted, never recreated (both same-process
  and cross-process journal-restart variants);
- process killed mid-create/update → resume completes from real state;
- stale-id delete refuses to remove a recreated resource;
- exhaustion distinguished from transient compute failure with bounded
  retries and recorded attempts;
- SQLite durability across a close/reopen;
- HTTP status mapping for every failure category.

Expected results are hand-authored in the tests; they are not generated by
the code under test.

## Key trade-offs and scope limits

- **Simulation, not a real cloud.** The provider is an in-process fake with a
  mutex-serialized control plane. It models the semantics that matter here
  (physical ids, idempotent create-by-name, capacity, ambiguous outcomes) but
  there is no real network, eventual-consistency lag or authorization layer.
- **Logical-name adoption on create.** A create adopts an already-present
  resource with the same logical key only after a real attempt (or when it
  was in the plan baseline). On the first attempt a brand-new, non-baseline
  resource is not claimed — the provider arbitrates. This prevents a
  coincidental name collision from masking an exhaustion/conflict.
- **Drift gate is whole-world at apply start**, with an exemption for
  resources whose own operation already started during a crashed run. It is
  not a continuous background watcher.
- **Single SQLite connection / single process.** Journal writes are serial;
  this does not implement multi-writer leases or a distributed lock.
- **Fixed retry budget** (default 3 attempts/op). Exhaustion and transient
  compute errors are retried within it; state conflicts fail immediately.
- References resolve through already-created resources in the run, falling
  back to baseline ids; there is no cross-run import/discovery beyond that.
