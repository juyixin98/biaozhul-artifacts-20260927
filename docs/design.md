# Design notes

## Components and ownership

```
        create/update/delete (HTTP)                 reconcile (HTTP)
 user ───────────────────────────▶  desired API  ◀────────────────── controller
                                     SQLite A                            │
                                     generation / resourceVersion        │ HTTP
                                     status / finalizers                 ▼
                                                                actual resource service
                                                                     SQLite B (independent)
                                                                     id / ownerUID / version
                                                                  + fault injection admin API
                                                                          ▲
 controller keeps its OWN SQLite C: attempts, last-accepted actual       │ HTTP (admin)
 version, decision ledger — never opens A or B directly         tests / operator ──────┘
```

- **SQLite A** — desired plane truth (`internal/store`).
- **SQLite B** — physical resource truth and the independent verification
  oracle (`internal/actualstore`).
- **SQLite C** — controller-private bookkeeping (`internal/controllerstore`).

Three databases is deliberate: a test (or a reviewer) can read B directly to
count real resources and read C's ledger to see decisions, without trusting
the controller's own status writes to A.

## Reconcile state machine

```
                    ┌──────────────────────────────────────────────┐
                    │ get desired object (fresh read each round)    │
                    └───────────────┬──────────────────────────────┘
                                    │
                    deletionTimestamp set?
                      ┌──────────────┴───────────────┐
                     yes                            no
                      │                              │
   external id known? GET actual        finalizer present?
        ┌─────────────┴────────────┐        no → add (guarded) → requeue
       yes (404=gone)             present                            │
        │ GET: exists → DELETE ────┘      locate external by id/owner ── create if absent
        │   delete-failed  → Transient, requeue backoff         lost POST → by-owner claim
        │   response-lost  → ResponseLost, confirm next round            │
        │   gone/204       → continue                            observation gating:
        ▼                                                          ahead → refuse, requeue
   finalizer present? → remove (guarded) → requeue                 stale version → StaleObservation
        │ no                                                         equal/new → continue
        ▼                                                                  │
   DELETE desired (idempotent; 404 once purged)                gen/hash behind? → conditional PUT
        ▼                                                          412 → Conflict, requeue
   clear local state, Settled                                     in sync → idempotent status
```

Expected multi-round transitions (finalizer just added, update just applied,
external just deleted) requeue without touching the retry/backoff counter;
only genuine failures increment backoff.

## Resource ownership by phase (delete)

| Phase | Desired record (A) | Finalizer | Physical resource (B) |
|---|---|---|---|
| created, not yet reconciled | exists, gen=1, obs=0 | absent | absent |
| finalizer added | exists | present | (creating) |
| converged | exists, obs=gen | present | 1 row, ownerUID=uid |
| user DELETE, external delete failing | terminating | present | 1 row (kept) |
| external delete succeeded | terminating | present | absent |
| finalizer removed | terminating | absent | absent |
| purged | absent | — | absent |

## Idempotency keys

- Actual create is unique on `owner_uid`. A second create returns the
  existing row with 409 rather than inserting another.
- Actual update/delete are conditional on `version`; the controller sends the
  version it last observed and re-reads on 412.
- After a lost mutating response the controller never blindly retries the
  mutation: create → `GET /by-owner/{uid}`, delete → `GET {id}` to confirm.

## Why a stored "last accepted actual version"

A stale read (`stale-get`) looks like a normal read at the HTTP level. The
controller can only know it is stale by remembering, in SQLite C, the highest
actual `version` it has already acted on. A served snapshot at or below that
version (or any version below it) is classified `StaleObservation`; the
header `X-Current-Version` is corroborating evidence but the decision does
not rely on trusting the faulty server's framing alone.

## Optimistic concurrency rules (desired plane)

- spec/finalizer/status writes require the current `resourceVersion`;
  mismatch → 409.
- `observedGeneration > generation` → 422 refused.
- `observedGeneration < recorded observedGeneration` → 409 (never regress).
- purge with remaining finalizers → refused.
