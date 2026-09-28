# zcluster API 请求样例

所有端点运行在 `http://127.0.0.1:8000`。建议每个请求带 `X-Request-Id` 头；
不带时服务端生成 `req-…`，响应头原样回显，日志与审计行都用它关联。

## 1. 健康检查 / 版本

```bash
curl -s http://127.0.0.1:8000/health -H 'X-Request-Id: demo-health' | python -m json.tool
```

## 2. 定义模式（每维位宽 + 有符号，此后固定不可改）

```bash
curl -s -X POST http://127.0.0.1:8000/api/schema \
  -H 'Content-Type: application/json' -H 'X-Request-Id: demo-schema' \
  -d '{
        "name": "points3d",
        "dimensions": [
          {"name": "x", "bits": 16, "signed": true},
          {"name": "y", "bits": 16, "signed": true},
          {"name": "z", "bits": 12, "signed": false}
        ]
      }' | python -m json.tool
```

`total_interleaved_bits = 16*3 = 48 ≤ 64` → code 列用 uint64；
改为 3×24 位（72 位）则自动切换为定长大端二进制列。

## 3. 入库（坐标越界逐行报告，整批无效时 400 + 失败类别）

```bash
curl -s -X POST http://127.0.0.1:8000/api/ingest \
  -H 'Content-Type: application/json' -H 'X-Request-Id: demo-ingest' \
  -d '{"rows": [
        {"x": -32768, "y": -32768, "z": 0},
        {"x": -1, "y": 0, "z": 100},
        {"x": 0, "y": -1, "z": 4095},
        {"x": 32767, "y": 32767, "z": 4095}
      ]}' | python -m json.tool
```

## 4. 范围查询（保守区间 + 精确残余过滤；返回步骤、区间、统计）

```bash
curl -s -X POST http://127.0.0.1:8000/api/query \
  -H 'Content-Type: application/json' -H 'X-Request-Id: demo-query' \
  -d '{
        "box": [
          {"dimension": "x", "lo": -100, "hi": 100},
          {"dimension": "y", "lo": -100, "hi": 100},
          {"dimension": "z", "lo": 0, "hi": 4095}
        ],
        "interval_budget": 256
      }' | python -m json.tool
```

关键字段：

- `box.unsigned` — 有符号→保序无符号映射后的盒边（负数映射可在此核对）；
- `intervals[].exact=false` + `budget_exhausted=true` — 预算耗尽，退化为全域保守区间（不漏，但膨胀）；
- `stats.candidate_rows` / `result_rows` / `false_positive_rows` — 候选膨胀；
- `stats.chunks_read` / `chunks_skipped` / `bytes_read` — 分块读取量；
- `steps[]` — 每一步（映射、分解、扫描块数、残余过滤）；
- `uncertainties[]` — 不确定结论单列。

## 5. 预算耗尽（允许扩大候选，不漏结果）

```bash
curl -s -X POST http://127.0.0.1:8000/api/query \
  -H 'Content-Type: application/json' -H 'X-Request-Id: demo-query-b1' \
  -d '{"box": [{"dimension": "x", "lo": -100, "hi": 100},
                {"dimension": "y", "lo": -100, "hi": 100},
                {"dimension": "z", "lo": 0, "hi": 4095}],
       "interval_budget": 1}' | python -m json.tool
```

## 6. 全扫描对照（参考答案路径，用于零漏行核对）

```bash
curl -s -X POST http://127.0.0.1:8000/api/full-scan \
  -H 'Content-Type: application/json' -H 'X-Request-Id: demo-scan' \
  -d '{"box": [{"dimension": "x", "lo": -100, "hi": 100},
                {"dimension": "y", "lo": -100, "hi": 100},
                {"dimension": "z", "lo": 0, "hi": 4095}]}' | python -m json.tool
```

## 7. 重写聚簇（行身份稳定）/ 块清单 / 审计

```bash
curl -s -X POST http://127.0.0.1:8000/api/compact -H 'X-Request-Id: demo-compact' | python -m json.tool
curl -s http://127.0.0.1:8000/api/chunks | python -m json.tool
curl -s http://127.0.0.1:8000/api/requests/demo-query | python -m json.tool
curl -s http://127.0.0.1:8000/api/requests?limit=20 | python -m json.tool
```

## 8. 内建验证套件（临时合成夹具，不动线上数据集）

```bash
curl -s -X POST http://127.0.0.1:8000/api/verify -H 'X-Request-Id: demo-verify' | python -m json.tool
```
