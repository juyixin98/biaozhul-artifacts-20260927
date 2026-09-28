# Unicode 压缩 Trie 补全后端

基于 **Python 3.12 + FastAPI + SQLite** 的前缀补全服务：

- **规范化版本固定**：索引键 = `NFKC(原文).casefold()`，版本号 `norm-v1`
  写死并随数据持久化；展示始终保留用户写入的**原文**。
- **压缩 Radix Trie**：共享长前缀的边被压缩为字符串标签，节点数远小于朴素 Trie。
- **可靠子树上界**：每个节点维护 `max_score` 与最优排序元组 `best`；
  插入、词频更新、热词降权、删除后沿父链重算，上界永不陈旧。
- **精确 top-k**：best-first 分支限界，按子树上界展开；等于阈值的同分
  候选**绝不剪枝**，同分按稳定规范键 `(term_norm, display, id)` 决胜。
  查询只展开前缀子树，**不遍历全词典再排序**。
- **持久快照**：每次写入/删除生成版本（父版本链）；快照物化全量词条，
  支持历史版本只读查询与一键恢复。

数据全部为本地合成夹具，无任何外部业务账号或网络依赖。

---

## 目录结构

```
app/
  normalize.py   文本规范化（固定版本 v1：NFKC -> casefold；稳定规范键）
  trie.py        算法索引（压缩 Radix Trie、可靠上界、精确 top-k、不变量校验）
  storage.py     版本存储（SQLite：versions/entries/entry_events/snapshots）
  engine.py      领域服务（锁、索引重建、快照缓存、降级语义、诊断）
  api.py         FastAPI 路由与统一错误处理（request_id 关联日志）
  schemas.py     请求/响应 Pydantic 模型
  errors.py      领域错误与错误码
  config.py      配置层（环境变量）
  main.py        可运行服务入口（uvicorn）
tests/
  reference.py   独立参考实现（全量筛选 + 硬编码 Unicode 字面值预言）
  test_normalize.py
  test_trie.py
  test_random_crosscheck.py   随机操作序列 vs 独立参考模型
  test_engine_storage.py
  test_api.py
  conftest.py    测试夹具、run-id 日志关联
scripts/demo.py  本地端到端演示（五类场景 + 快照/恢复）
requirements.txt
```

---

## 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 1) 直接运行测试（会实际执行并报告结果）
pytest

# 2) 本地端到端演示（合成数据，打印剪枝依据与版本链）
python scripts/demo.py

# 3) 启动 HTTP 服务
CTRIE_DB_PATH=data/ctrie.db python -m app.main --host 127.0.0.1 --port 8000
#   或：uvicorn app.main:app --port 8000
```

配置环境变量：`CTRIE_DB_PATH`（默认 `data/ctrie.db`）、
`CTRIE_TOPK_MAX`（默认 100）、`CTRIE_LOG_LEVEL`（默认 `INFO`）。

### 最小调用示例

```bash
curl -s -X POST localhost:8000/api/v1/entries:bulkUpsert \
  -H 'Content-Type: application/json' \
  -d '{"entries":[{"id":"a","term":"ＣＡＦＥ","score":9},{"id":"b","term":"cafe","score":9}]}'

curl -s "localhost:8000/api/v1/complete?prefix=cafe&k=5&diagnostics=true"
```

---

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 存活检查 |
| GET  | `/api/v1/complete?prefix=&k=&version=&diagnostics=` | 前缀补全（空前缀合法 = 全局 top-k） |
| POST | `/api/v1/entries:bulkUpsert` | 批量插入/词频更新（整批原子） |
| POST | `/api/v1/entries:delete` | 删除词条（不存在 404） |
| GET  | `/api/v1/versions?limit=` | 版本链 |
| POST | `/api/v1/snapshots` | 对当前 HEAD 建物化快照 |
| GET  | `/api/v1/snapshots` | 快照列表 |
| POST | `/api/v1/snapshots:restore` | 从快照恢复（生成 restore 版本） |
| GET  | `/api/v1/status` | 结构/词条数/深度/健康度 |
| GET  | `/api/v1/diagnostics/invariants` | 独立重算 Trie 不变量并列出违规 |

每个响应都可通过请求头 `X-Request-ID` 关联（未提供则服务端生成），
同名响应头回传；服务日志逐行携带该 ID、当前 HEAD 版本与方法路径。
`diagnostics=true` 时补全响应额外包含每一步展开/剪枝记录：
子树上界分值、当前阈值、判定（`expand`/`prune`）与文字依据。

---

## 错误语义（异常绝不伪装成功）

| 情况 | HTTP | `error_code` |
|---|---|---|
| 请求体/参数 Pydantic 校验失败（空 term、多余字段、类型错） | 400 | `VALIDATION_ERROR` |
| 分值为 NaN / ±Infinity；`k<1` 或 `k>CTRIE_TOPK_MAX`；批次内 id 重复 | 400 | `VALIDATION_ERROR` |
| 规范化后键为空 | 400 | `VALIDATION_ERROR` |
| 删除不存在的词条 | 404 | `ENTRY_NOT_FOUND` |
| 版本不存在；查询未物化的普通 commit 版本；恢复不存在的快照 | 404 | `VERSION_NOT_FOUND` |
| 数据库的规范化版本与代码不兼容（拒绝静默重解释历史数据） | 409 | `NORMALIZER_MISMATCH` |
| 存储已提交但内存索引更新/不变量失败：实例置 **degraded**，拒绝继续查询 | 500 | `INDEX_DEGRADED` |
| 其他未预期异常：完整堆栈与 `request_id` 写日志，响应只回错误 ID | 500 | `INTERNAL_ERROR` |

批量写入是**整批原子**：任一条非法则整批拒绝，无部分写入。

---

## 关键正确性设计

### 上界为何可靠（热词删改后不能错误剪枝）

每个节点 `n` 始终满足：

```
n.max_score = max( 本节点终止词条最高分, max(c.max_score for c in children) )
n.best      = min_rank( 本节点最优 rank, min(c.best for c in children) )
rank       = (-score, (term_norm, display, entry_id))
```

- 同 id 写入（改分/改名）走 upsert：键不变则原地替换并沿父链重算；
  规范化键改变则先从旧终止节点摘除（必要时压缩度-1链）再插入新位置，
  **旧子树上界同步收缩**。
- 删除后自底向上重算整棵树并压缩，保证不存在陈旧的度-1无词条节点。
- 剪枝判定为严格比较：子树最优 rank `>= 当前第 k 名 rank` 才剪。
  rank 末位是唯一 `entry_id`，相等不可能，因此等于阈值的同分候选不会被误剪。
- `/api/v1/diagnostics/invariants` 独立重算所有节点的上界并与缓存值、
  存储词条数比对；随机测试每一步变更后都调用它。

### 查询复杂度

先 `locate(prefix)`（沿压缩边匹配，O(前缀长度)），再在**前缀子树**内
best-first 展开堆；上界低于阈值的整棵子树一次剪枝。诊断轨迹中的
`terminals_seen`（扫描终止词条数）在测试中被断言远小于全词典大小。

### 同分稳定序

`score` 降序后，依次按 `term_norm`、展示原文 `display`（Unicode 码位序）、
`entry_id` 升序，构成全序；插入顺序不影响结果（有专门测试用逆序插入验证）。

---

## 测试与复现

测试不是“接口能调用”式断言，而包含：

- **硬编码 Unicode 字面值**（11 条，含全角、`ß`、连字 `ﬁ`、`①`、希腊字母、
  土耳其 `İ`）手工锁定规范化结果，并与一份**独立重写**的参考实现比对；
- **独立朴素预言** `tests/reference.py`：全量遍历 + `sorted` 得到精确答案，
  被测 Trie / Engine 的查询逐项（含顺序）与之相等；
- 5 个固定种子的 400 步随机操作序列（插入 / 词频更新 / 改名迁移 / 删除），
  每步校验不变量、根上界=真实最高分、随机前缀 top-k 与预言一致；
- 白盒剪枝审计：任一条剪枝记录对应的子树中，所有词条分值都不超过阈值；
- 长公共前缀压缩率、同分次序、热词降权整枝剪枝、规范化碰撞、空前缀、
  持久化重启、快照历史查询与恢复、全部 HTTP 错误类别。

失败用例的断言消息带有**失败类别**前缀（如 `失败类别: 根上界陈旧`）。

```bash
# 运行并指定可关联的运行身份（日志写到 test-runs/<run-id>.log）
pytest --run-id=myrun-001

# 复现某次随机失败：种子是固定参数
pytest tests/test_random_crosscheck.py -k 20260927

# 只跑端到端演示并观察剪枝依据
python scripts/demo.py
```

日志内容包含：run-id、节点 id、版本号、入堆/展开/剪枝/扫描计数、
每条剪枝的上界与阈值及判定理由。
