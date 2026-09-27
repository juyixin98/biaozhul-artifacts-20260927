# HTTP API

Base URL: wherever `ia serve` binds (default `http://127.0.0.1:8080`).
All request/response bodies are JSON. Log lines on the server's stderr carry
the same `request_id` as the response envelope.

## `GET /healthz`

```json
{ "status": "ok", "service": "interval-analysis-service",
  "service_version": "0.1.0", "solver_version": "0.1.0",
  "lang_version": "0.1.0" }
```

## `POST /api/analyze`

Request fields (all optional except `source`):

| field | type | meaning |
|---|---|---|
| `source` | string | IAL program text |
| `request_id` | string \| null | caller correlation id; generated when absent |
| `narrowing` | bool \| null | apply descending narrowing after widening (default true) |
| `plain_fixpoint` | bool \| null | Kleene iteration without widening (default false) |
| `max_trace_events` | number \| null | cap on trace events (default 200) |

`200` with the envelope and a data payload; `422` when parsing/validation
fails (`ok:false`, `diagnostics` populated, no `data`).

## `POST /api/verify`

| field | type | meaning |
|---|---|---|
| `source` | string | IAL program text |
| `request_id` | string \| null | correlation id |
| `enumeration_cap` | number \| null | max input combinations (default 200000) |
| `step_limit` | number \| null | per-execution concrete step budget (default 200000) |
| `narrowing` | bool \| null | narrowing for the embedded analysis (default true) |

`data` contains `sound`, `combinations_run`, `combinations_declared`,
`enumeration_complete`, per-category concrete failure counts, per-site
`abstract_sites` observations and any `violations`. If the declared domain
exceeds the cap the endpoint returns `422` with a diagnostic whose text ends
in **"exhaustive check NOT run"** — this is deliberately not a success.

## Verdict and certainty values

* `verdict`: `safe` | `possible_failure` | `guaranteed_failure` | `unreachable`
* `certainty` (only on failing verdicts): `possible` | `guaranteed`

`possible_failure` explanations always contain the word
"over-approximation" to prevent misreading an uncertain result as a proven
defect.

## Example

```bash
curl -s -XPOST localhost:8080/api/verify \
  -H 'content-type: application/json' \
  -d '{"source":"input s [0:6]; { i=s; while(i>=0){i=i-1;} assert(i==-1); }"}'
```
