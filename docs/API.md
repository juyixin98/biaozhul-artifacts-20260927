# HTTP API

所有响应含请求关联头 `X-Correlation-ID`（可在请求头自定义）。
错误体形如：

```json
{"error": {"code": "ROW_WIDTH_MISMATCH", "message": "…",
           "details": {"row_index": 1, "expected_width": 3, "actual_width": 2},
           "correlation_id": "req-…"}}
```

未知异常返回 500/`INTERNAL_ERROR` 且**不**返回成功体。

## 元信息

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 状态、版本、metric_version、是否临时密钥、运行数 |
| GET | `/version` | 服务/指标/结构版本 |

## 运行

### POST /runs
请求体：

| 字段 | 说明 |
|---|---|
| `columns` | 全部列名（非空、唯一） |
| `rows` | 行数组，按 columns 排序；`null`/空串/纯空白=缺失值 |
| `quasi_identifiers` | **显式** QI 列（至少 1） |
| `sensitive` | **显式**敏感列（至少 1） |
| `hierarchies` | 每个 QI 列的 `levels`；第 0 级恒为原值 |

层级规则（判别字段 `rule`）：

- `{"rule":"map","mapping":{"a":"ab",...}}`：显式“上级标签→本级标签”。
- `{"rule":"prefix","keep":4}`：字符串前缀；多个 prefix 级 keep 必须严格递减。
- `{"rule":"range","bins":[0,30,120],"labels":["<30","30+"]}`：左闭右开
  数值箱，右端点并入最后一箱；各级箱必须比上一级粗（保持包含）。

201 响应：`run_id`、`access_token`（只返回这一次）、`null_counts`、
`hierarchy_validation`（每级标签数、是否含 `*`、NULL 是否保留）。

### POST /runs/csv
`multipart/form-data`：`file`（UTF-8 CSV，首行表头）、`quasi_identifiers`、
`sensitive`（逗号分隔）、`hierarchies`（JSON 字符串）。空单元格=NULL。

### GET /runs · GET /runs/{id} · DELETE /runs/{id}
删除需 `X-Run-Token`，成功返回 204。

### GET /runs/{id}/operations
需令牌。返回该运行内每次 evaluate/suggest 的状态与聚合摘要。

## 风险

### POST /runs/{id}/evaluate?k=2&l=2
body：`{"levels": {"zip": 1, "age": 2}}`（缺省列按 0）。

报告关键字段：

- `k_anonymized` / `l_diverse` / `risk_overall`；
- `classes[]`：`size`、`prosecutor_risk=1/size`、`risk_category`、
  `k_ok`、`l_ok`、`reasons[]`（机器可读判定依据）、
  `sensitive_distinct_non_null`、`sensitive_null_members`、
  `sensitive_frequency_histogram`（次数→类数，**无敏感值文本**）、
  `class_fingerprint`（HMAC 截断指纹）、`qi_key_contains_null`；
- `rows_in_violating_classes`、`class_size_distribution`（大小→行数）、
  `null_qi_rows`、`null_counts_by_column`；
- `discernibility`（DM=Σsize²）、`loss_metric`（LM）、`metric_version`、
  `disclaimer`。

### POST /runs/{id}/suggest
body：`{"k": 2, "l": 2}`。

- 可行：`status="FEASIBLE"`、`levels`、`class_sizes`、`discernibility`、
  `loss_metric`、`evaluated_vectors/total_vectors`、`verdict_basis`。
- 不可达：HTTP 200 但 `status="UNREACHABLE"`，`unreachable_evidence`
  给出全泛化时的违规类聚合（大小、非 NULL 敏感值种数）与全局不同敏感值数。
- 格点过大：400 `LATTICE_TOO_LARGE`（不做近似）。

## 审计（管理）

`GET /audit/events?run_id=&status=&limit=&offset=`，需 `X-Admin-Token`。
事件状态枚举：`SUCCESS / FAILURE / UNREACHABLE / REFUSED / VALIDATION_ERROR`。
审计表为只追加（触发器拒绝 UPDATE/DELETE）。

## 错误码（节选）

`EMPTY_*`、`COLUMN_NOT_FOUND`、`DUPLICATE_COLUMN_ROLE`、
`ROW_WIDTH_MISMATCH`、`HIERARCHY_MISSING/BAD_LEVEL/INCOMPLETE/NOT_CONTAINING/
UNKNOWN_KEY/KEEP_NOT_DECREASING/LEVEL_NOT_FOUND`、
`THRESHOLD_UNREACHABLE`、`LATTICE_TOO_LARGE`、
`RUN_NOT_FOUND`、`RUN_FORBIDDEN`、`UNAUTHORIZED`、`INTERNAL_ERROR`。
