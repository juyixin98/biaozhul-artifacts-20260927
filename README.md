# Two-Level Pruning Backend

A partition + file-statistics query pruning backend. Given a predicate, it
decides which **directory partitions** and then which **files** cannot contain
a matching row, without reading row data. Every elimination is provably safe:
an independent full-scan reference checks that **zero matching rows are ever
pruned**.

Stack: Python 3.12 · FastAPI · PyArrow (Parquet) · SQLite. All data is local
and synthetically generated — no external accounts or services.

---

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt      # exact, pinned versions
```

Pinned versions that matter for correctness (see `requirements.txt`):

| component | pinned | why |
|---|---|---|
| `pyarrow` | 20.0.0 | Parquet statistics semantics / scanner kernels |
| `tzdata` | 2024.2 | IANA time-zone database used to derive month buckets |

The transform spec version is `month-tz-v1` (code constant). The plan reports
all three versions and refuses to prune if the catalog was built with a
different transform spec **or** a different tzdb.

## 2. Build the demo data and catalog

```bash
# 1) write the synthetic Parquet tree under data/events/
python3 scripts/make_fixtures.py --root data/events

# 2) scan it and (re)build the SQLite metadata catalog transactionally
python3 -m prune.cli --config config/prune.toml refresh
```

Layout produced (partition directories are `YYYY-MM` labels in
**Asia/Shanghai**, plus an explicit `__null__` label):

```
data/events/
  1969-12/  neg_a.parquet neg_b.parquet       # negative (pre-1970) timestamps
  2024-02/  feb_real.parquet                  # amount column all NULL
  2024-03/  mar_early.parquet                 # Mar 04..Mar 05
            mar_mid.parquet                   # Mar 10..Mar 11
            mar_boundary.parquet              # Mar 31..Apr 01
  2024-04/  apr.parquet
  2024-05/  may_truncstr.parquet
            may_truncstr.parquet.stats-override.json   # truncated UTF8 max
  __null__/  null_ts.parquet                  # all ts NULL, stats present
             no_stats.parquet                 # statistics block disabled
```

The `.stats-override.json` sidecar emulates a foreign writer that only
persisted a **truncated prefix** for the string `max` statistic, without
replacing the Parquet engine.

## 3. Run the API

```bash
python3 -m prune.cli --config config/prune.toml serve --host 127.0.0.1 --port 8000
```

Endpoints:

| method | path | purpose |
|---|---|---|
| GET | `/health` | service + pinned versions |
| GET | `/tables` | configured / cataloged tables |
| GET | `/tables/{name}` | columns, transform, partitions, per-file stats versions |
| POST | `/tables/{name}/prune` | pruning plan with per-target verdicts and reasons |
| POST | `/tables/{name}/validate` | plan **plus** an independent full-scan zero-miss check |

Every response carries `request_id` (honors an inbound `X-Request-Id`), and
splits uncertain conclusions (`uncertain`) from hard failures (`failures`).

### Example — one Shanghai-local day, March 5 2024

```bash
curl -s -X POST localhost:8000/tables/events/prune \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-day-range' \
  -d '{"op":"AND","children":[
        {"op":"GE","column":"ts","value":"2024-03-05T00:00:00+08:00"},
        {"op":"LT","column":"ts","value":"2024-03-06T00:00:00+08:00"}]}'
```

Result (abridged): level 1 keeps only partition `2024-03`; level 2 then prunes
two of its three files using min/max stats:

```
candidate_partitions: {lo: 2024-03, hi: 2024-03, null_bucket: EXCLUDED}
1969-12 PRUNED PARTITION_OUTSIDE_CANDIDATES
2024-02 PRUNED PARTITION_OUTSIDE_CANDIDATES
2024-03 KEPT
   mar_early.parquet     KEPT
   mar_mid.parquet       PRUNED FILE_BELOW_MIN
   mar_boundary.parquet  PRUNED FILE_BELOW_MIN
2024-04 PRUNED PARTITION_OUTSIDE_CANDIDATES
2024-05 PRUNED PARTITION_OUTSIDE_CANDIDATES
__null__ PRUNED PARTITION_NULL_EXCLUDED
metrics: 5/6 partitions pruned, 9/10 files, 20/23 rows, 11982/13384 bytes
```

### Example — validate against a full scan

```bash
curl -s -X POST localhost:8000/tables/events/validate \
  -H 'Content-Type: application/json' \
  -d '{"op":"BETWEEN","column":"ts",
       "value":["1969-12-10T00:00:00+08:00","1969-12-31T23:59:59+08:00"]}'
# validation: {"ok": true, "failure_category": null,
#              "expected_matching_rows": 3, "missed_ids": []}
```

### Predicate wire form

Tree of `AND` / `OR`; leaves are column predicates:

```json
{"op":"AND","children":[
  {"op":"GE","column":"ts","value":"2024-03-05T00:00:00+08:00"},
  {"op":"LT","column":"ts","value":"2024-03-06T00:00:00+08:00"},
  {"op":"BETWEEN","column":"amount","value":[10,50]},
  {"op":"IN","column":"name","value":["a","b"]},
  {"op":"IS_NULL","column":"ts"},
  {"op":"IS_NULL","column":"ts","negated":true}
]}
```

Leaf ops: `EQ NE GT GE LT LE BETWEEN IN IS_NULL`. Comparison semantics follow
SQL three-valued logic (comparisons reject NULL; `IS NULL` only matches NULL).

## 4. Test

```bash
python3 -m pytest -q
```

The suite (97 tests) asserts **concrete** outcomes and failure categories, not
just that endpoints respond:

* transform math across negative epoch / zone boundaries / pinned versions;
* exact file verdicts and reason codes (`FILE_ABOVE_MAX`, `FILE_NO_NULL`,
  `FILE_STATS_TRUNCATED`, `FILE_ALL_NULL`, …);
* end-to-end **zero missed rows** against the independent PyArrow full scan,
  including 40 randomized predicates;
* catalog transaction rollback and the version-mismatch conservative guard;
* HTTP status codes, request-id correlation, separated failures/uncertainty.

See `docs/ARCHITECTURE.md` for the reasoning rules and `docs/LIMITATIONS.md`
for what is deliberately out of scope.
