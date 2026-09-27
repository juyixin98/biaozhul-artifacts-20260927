# merge3 — 结构保留的三方文本合并后端

基于共同基线的三方文本合并服务:基线、本地、远端三份文本生成区间编辑,
互不相交自动合并,冲突携带三方源范围并可按显式选择重建。行结束符与末尾
换行不被静默改写。

## 结构

```
app/
  textnorm.py     文本规范:行切分、行结束符与末尾换行的显式建模
  edits.py        区间编辑:由共同基线生成半开区间编辑
  merge.py        三路合并算法:冲突规则、冲突块、显式重建
  store.py        版本存储:SQLite 持久化版本与合并记录
  diagnostics.py  诊断:带请求标识的结构化日志与脱敏
  api.py          查询接口:FastAPI 应用
  config.py       配置(环境变量,均有本地默认值)
fixtures/         可复用合成夹具(参考答案手写,非由被测实现生成)
tests/            单元与集成测试
scripts/verify.py 端到端验证脚本
docs/semantics.md 边界语义说明
```

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 测试与端到端验证
.venv/bin/python -m pytest
.venv/bin/python scripts/verify.py

# 启动服务(本地 SQLite,无需任何外部账号)
.venv/bin/uvicorn app.api:app --port 8000
```

配置通过环境变量:`MERGE3_DB_PATH`(默认 `merge3.sqlite3`)、
`MERGE3_LOG_PATH`(默认 `diagnostics.log`)、`MERGE3_LOCAL_LABEL` /
`MERGE3_BASE_LABEL` / `MERGE3_REMOTE_LABEL`。

## API 摘要

- `POST /merge` — 内联三方文本合并,返回状态、合并文本、冲突(含三方源范围)
- `GET /merges/{id}` — 查询合并记录与诊断说明
- `POST /merges/{id}/resolve` — 按显式选择重建冲突
- `POST /documents/{doc}/versions`、`GET /documents/{doc}/versions` — 版本存取
- `POST /documents/{doc}/merge` — 按已存版本号合并

边界语义(冲突规则、行结束符与末尾换行策略、失败类别)见
[docs/semantics.md](docs/semantics.md)。

## 已知警告

- `starlette 1.7.0` 的 TestClient 会对 `httpx` 发出弃用警告
  (建议未来的 `httpx2`);仅为警告,不影响测试结果。
