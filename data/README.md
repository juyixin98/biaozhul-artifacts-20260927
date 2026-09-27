# Sample API requests

All payloads are local synthetic data — no external accounts or real
business data. The `_comment` field is ignored by the server.

| File | Endpoint | Expected result |
|------|----------|-----------------|
| `equiv_accepted.json` | `POST /equiv` | `200`, `decision: accepted`, 4 rows checked, no witness |
| `equiv_rejected.json` | `POST /equiv` | `422`, `decision: rejected`, witness with `a != b` |
| `equiv_renamed.json` | `POST /equiv` | `200`, accepted under `{x->p, y->q}` |

Example:

```bash
curl -s localhost:8080/equiv \
  -H 'content-type: application/json' \
  --data @data/equiv_accepted.json | jq
```
