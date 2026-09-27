# 服务调用示例（curl）

先启动服务：

```bash
python scripts/init_db.py
uvicorn service.main:app --host 127.0.0.1 --port 8000
```

## 成功查询：字段 + 括号 + AND/OR

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"apple AND (pie OR salad)"}'
```

命中 `["d1","d2","d8","d9"]`，返回 `canonical`（规范树）、`version`（内容寻址版本号）、
`budget_usage`（深度/子句数）、`run_id`（关联日志）。

## 短语：引号内 OR 是词，不是运算符

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"title:\"a OR b\""}'
```

## 空查询 = 全部文档

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' -d '{"query":""}'
```

## 等价书写版本号一致

```bash
curl -s -X POST http://127.0.0.1:8000/query -d '{"query":"apple AND pie"}' -H 'Content-Type: application/json'
curl -s -X POST http://127.0.0.1:8000/query -d '{"query":"pie AND apple"}' -H 'Content-Type: application/json'
# 两者 version 相同
```

## 异常查询（按类别区分，不会统一成 200）

```bash
curl -i -X POST http://127.0.0.1:8000/query -H 'Content-Type: application/json' \
  -d '{"query":"a AND OR b"}'   # 400 PARSE_ERROR，position=7
curl -i -X POST http://127.0.0.1:8000/query -H 'Content-Type: application/json' \
  -d '{"query":"foo:bar"}'      # 422 FIELD_UNKNOWN，position=1
curl -i -X POST http://127.0.0.1:8000/query -H 'Content-Type: application/json' \
  -d '{"query":"year:abc"}'     # 422 FIELD_TYPE，position=1
curl -i -X POST http://127.0.0.1:8000/query -H 'Content-Type: application/json' \
  -d '{"query":"\"oops"}'        # 400 LEXER_ERROR，position=1
```

## 查询版本登记

```bash
curl -s http://127.0.0.1:8000/queries/50dd4ea45067b566
```

Python 版见 `examples/client.py`（可用 `SEARCHDSL_URL` 改地址）。
