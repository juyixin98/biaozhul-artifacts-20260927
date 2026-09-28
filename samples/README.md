# 样例请求

`POST http://127.0.0.1:8080/v1/requests`，`Content-Type: application/json`。

| 文件 | 场景 | 预期 |
| --- | --- | --- |
| `request-create-minimal.json` | 空 spec 的 CREATE | 200；默认值补齐 replicas/schedule，capacity 由 replicas 推导，盖 UID 注解 |
| `request-create-replicas.json` | 指定 replicas=3 的 CREATE | 200；capacity=300 |
| `request-create-toomany.json` | replicas=99 | 422 validation_denied（超过单对象上限 5），响应带被拒对象摘要 |
| `request-update-scaleup.json` | orders 3→4 | 200；配额只占 +1 增量，capacity=400 |

快速验证：

```bash
for f in samples/*.json; do
  printf '%-45s ' "$f"
  curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/v1/requests \
    -H 'Content-Type: application/json' -d @"$f"
done
```

内联构造其它类别：

```bash
# 400 invalid_input
curl -s -X POST localhost:8080/v1/requests -d '{"operation":"CREATE"}'

# 409 state_conflict（用两个不同 UID 创建同名资源；第二次）
curl -s -X POST localhost:8080/v1/requests -H 'Content-Type: application/json' -d '{
  "uid":"a","operation":"CREATE",
  "object":{"apiVersion":"v1","kind":"Widget","metadata":{"name":"dup","namespace":"ns"},"spec":{}}}'
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/v1/requests -H 'Content-Type: application/json' -d '{
  "uid":"b","operation":"CREATE",
  "object":{"apiVersion":"v1","kind":"Widget","metadata":{"name":"dup","namespace":"ns"},"spec":{}}}'
```
