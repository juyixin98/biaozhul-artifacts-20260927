# Example requests

All requests/responses are JSON. Every response carries an `X-Run-Id` header;
send your own `X-Run-Id` to correlate a retry chain.

## 1. Declare list semantics for a kind

```bash
curl -XPUT localhost:8080/v1/schemas/widget \
  -H 'content-type: application/json' \
  -d '{
    "lists": {
      "ingresses": {"type": "map", "key": "name"},
      "tags":      {"type": "set"},
      "servers":   {"type": "atomic"}
    }
  }'
```

Undelimited field names are dotted paths inside objects. Lists that appear in a
config without a declaration are rejected with `invalid_input/undeclared_list`.

## 2. Apply (manager `net`)

```bash
curl -XPOST localhost:8080/v1/widget/web-1/apply \
  -H 'content-type: application/json' \
  -d '{
    "manager": "net",
    "config": {
      "image": "registry/widget:1",
      "ingresses": [{"name": "edge-1", "host": "a.example", "port": 80}],
      "tags": ["canary"],
      "servers": [{"zone": "z1", "weight": 10}]
    }
  }'
```

`config` is the manager's FULL declaration:

- field present, non-null → set
- field present, `null` → explicit delete
- field absent → retract only fields this manager owns; foreign fields stay

## 3. Conflict (HTTP 409) — another manager touches `image`

```bash
curl -XPOST localhost:8080/v1/widget/web-1/apply \
  -H 'content-type: application/json' \
  -d '{"manager": "sre", "config": {"image": "registry/widget:2"}}'
```

```json
{
  "error": {
    "category": "conflict",
    "code": "ownership_conflict",
    "message": "1 field(s) owned by other managers; resubmit with force to take over",
    "details": {
      "conflicts": [
        {"path": "image", "owners": ["net"],
         "reason": "atomic_value_mismatch", "wanted": "set"}
      ],
      "preview": { "...": "merged state only if all conflicts were ignored" }
    }
  }
}
```

Nothing is committed on conflict: live, ownership and revision stay unchanged
and no history row is written.

## 4. Explicit force takeover

```bash
curl -XPOST localhost:8080/v1/widget/web-1/apply \
  -H 'content-type: application/json' \
  -d '{"manager": "sre", "force": true,
       "config": {"image": "registry/widget:2"}}'
```

Only `image` changes owner (`net` → `sre`); `ingresses`/`tags`/`servers` and
their ownership are untouched.

## 5. Inspect ownership and history

```bash
curl localhost:8080/v1/widget/web-1/ownership
curl localhost:8080/v1/widget/web-1/history
curl localhost:8080/v1/widget/web-1
```

History rows capture revision, manager, force flag, the submitted config, the
resulting live value, the change list (with before/after) and the run id —
enough to replay or audit any revision.

## Error categories → HTTP status

| category              | status | examples |
|-----------------------|--------|----------|
| invalid_input         | 400    | bad JSON, missing manager, undeclared list, duplicate set value |
| not_found             | 404    | unknown resource |
| conflict              | 409    | ownership conflict (path + owners in details) |
| resource_exhausted    | 413/503 | body/live too large, resource cap, reconcile queue full |
| unavailable           | 503    | adapter offline |
| compute_failure       | 500*   | stored live not parseable by the engine |
| internal              | 500    | sqlite fault, corrupt ownership/schema rows |

`*` compute failures are deliberately a distinct code from storage faults so
dashboards can tell corrupt in-state data apart from driver errors.
