# Service call examples

All examples use the shipped 12-document synthetic corpus. Start the
service first:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m searchdsl.cli reindex --config config.json
python -m searchdsl.cli serve --config config.json
# serving on http://127.0.0.1:8000
```

Interactive docs are at <http://127.0.0.1:8000/docs>.

## Health, schema, versions

```bash
curl -s http://127.0.0.1:8000/health | python -m json.tool
curl -s http://127.0.0.1:8000/schema | python -m json.tool
curl -s http://127.0.0.1:8000/versions | python -m json.tool
```

## Search (GET)

```bash
# implicit AND + field-qualified inclusive range
curl -s 'http://127.0.0.1:8000/search?q=quick%20AND%20(fox%20OR%20salmon)%20AND%20year%3A%5B2000%20TO%202020%5D&explain=true' \
  | python -m json.tool
```

## Search (POST)

```bash
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d '{"q": "\"quick brown\" OR body:价格", "limit": 5, "explain": true}' \
  | python -m json.tool
```

## Errors are structured (HTTP 400 + stable code + span)

```bash
# unknown field
curl -s -w '\nHTTP %{http_code}\n' \
  'http://127.0.0.1:8000/search?q=bogus%3Acat'
# -> {"detail":{"code":"FIELD_UNKNOWN","message":"...","pos":{"start":0,"end":9}}}

# unterminated phrase
curl -s -w '\nHTTP %{http_code}\n' \
  'http://127.0.0.1:8000/search?q=%22abc'
# -> {"detail":{"code":"UNTERMINATED_STRING", ... "pos":{"start":0,"end":4}}}

# complexity budget (depth)
curl -s -w '\nHTTP %{http_code}\n' \
  'http://127.0.0.1:8000/search?q=NOT%20NOT%20NOT%20NOT%20NOT%20NOT%20a'
```

## Fetch a previously stored canonical query

```bash
HASH=$(curl -s 'http://127.0.0.1:8000/search?q=fox%20OR%20dog' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["query_hash"])')
curl -s "http://127.0.0.1:8000/searches/$HASH" | python -m json.tool
```

## Equivalent CLI calls (no server needed)

```bash
python -m searchdsl.cli parse  'quick AND (fox OR salmon)' --config config.json
python -m searchdsl.cli search 'title:fox AND NOT tags:dog' --config config.json --explain
python -m searchdsl.cli diagnose 'year:[2000 TO 2010] AND fox' --config config.json --out runs/
```

Every response/diagnostic log carries:

* `run_id` — unique run identity;
* `versions` — package, DSL spec, index schema, corpus and schema hashes;
* `diagnostics.events` — ordered stages (`input`, `parse`, `validate`,
  `normalize`, `store`, `execute`) with progress `current/total`;
* `error.code` + `error.pos` on failure — failures never return
  `status: "ok"`.
