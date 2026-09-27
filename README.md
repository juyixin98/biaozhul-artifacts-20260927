# 持久 Posting 列表布尔查询服务（AND / OR / NOT + 短路求值）

在**显式版本化的有限文档全集**上执行持久 posting 列表的布尔查询，统计分块跳跃与执行开销，
并提供带请求身份的可解释诊断。技术栈：Python 3.10+ / FastAPI / SQLite，零外部服务依赖。

## 1. 核心语义与约束

| 约束 | 实现位置 |
| --- | --- |
| **NOT 相对于显式版本化文档全集**，定义为 `U(version) ＼ X`，全集有限，绝不补成无限整数集 | `app/query/engine.py::_eval_not`、`app/storage/version_store.py::universe` |
| **跳跃块上界只用于安全跳过**：仅当“整块上界 < 目标”才整块越过，目标可能落在块内时必须逐个核对，因此不会越过任何命中 ID | `app/postings/blocked_list.py::Cursor.skip_to` |
| **删除文档同步全集可见性**：删除在新版本置 `visible=0` 并移除 posting，NOT 也不会把被删文档补回来 | `app/storage/version_store.py::commit` |
| **每个结果 ID 唯一**：构造时排序去重 + 引擎出口断言 | `BlockedPostingList.from_ids`、`engine.execute` |
| **输出执行统计，不用数据库集合查询代替核心求交**：SQLite 只负责按词项取 posting，AND/OR/NOT 全部在 Python 合并算子里完成 | `app/postings/operators.py` |
| **短路求值**：AND 中间结果为空即停止；OR 并集覆盖整个全集即停止；被跳过的子节点不求值 | `app/query/engine.py` |

查询语法（运算符大小写不敏感，不支持省略运算符的隐式 AND）：

```
expr   := or_expr
or_expr   := and_expr ( OR and_expr )*
and_expr  := not_factor ( AND not_factor )*
not_factor := NOT not_factor | atom
atom   := TERM | "(" expr ")"
```

## 2. 模块划分（各自承担实际工作，无硬编码演示）

```
app/
  config.py                 配置（环境变量可覆盖）
  postings/                 算法索引
    blocked_list.py         分块 posting 列表、块上界、跳跃游标、OpStats
    operators.py            intersect / union / difference / intersect_scan
  storage/
    version_store.py        SQLite 显式版本化全集、词项目录、copy-on-commit 版本
  query/
    spec.py                 词法/语法（文本规范）、AST、失败类别 ErrorCategory
    engine.py               AST 求值、短路、顺序参数、统计、轨迹
  diagnostics/              请求身份、JSON 结构化日志、轨迹环形缓冲
  api/                      FastAPI 路由、统一错误分类
scripts/
  generate_fixture.py       生成本地合成夹具（参考答案用独立集合代数推导）
  seed_db.py                夹具播种进 SQLite
tests/                      独立单元测试 + HTTP 集成测试
samples/fixture.json        合成夹具与独立参考答案
```

### 为什么说参考答案是独立的？

`samples/fixture.json` 中的 `expected_v2 / expected_v3 / expected_v1` 由
`scripts/generate_fixture.py` 用**内建 `set` 运算**从原始词项映射直接推导
（`cat & dog`、`U - cat` 等），该脚本**不导入 `app.postings`**。
测试把被测核心的输出与这份外部预言逐条比对，而非用核心自身生成答案再自证。

夹具覆盖题目要求的场景：

- **稀疏列表**：`cat=[1,9,17,25,33]`（每块至多一个点）
- **稠密列表**：`dog=[1,4,7,...,40]`（14 个点，步长 3）
- **空全集**：v1（初始版本，0 个文档）——任何有限补集仍为空
- **全否定**：词项 `everything` 覆盖整个全集，`NOT everything = ∅`
- **删除后查询**：v3 在 v2 上删除 `[9, 25, 40]`，验证全集可见性同步
- **执行顺序一致性**：同一查询分别用 `left_to_right` / `right_to_left` 求值；
  另有用 `intersect_scan` 的构造性用例证明“结果相同而跳过块数随顺序变化（3 vs 1）”

## 3. 首次运行

```bash
cd /home/admin/Downloads/xinbiaozhu/opp247/b
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 生成合成夹具 + 播种 SQLite（data/index.sqlite3）
.venv/bin/python scripts/generate_fixture.py
.venv/bin/python -m scripts.seed_db

# 启动（或直接 ./run.sh）
.venv/bin/uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

可用环境变量：`POSTING_DB_PATH`、`POSTING_LOG_PATH`、`POSTING_BLOCK_SIZE`（默认 8）、
`POSTING_AUTO_SEED`、`POSTING_TRACE_RING`（默认 256）。

启动后：交互文档 `http://127.0.0.1:8000/docs`。

## 4. API 速览

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查 + 最新版本号 |
| `POST /query` | 执行布尔查询（body：`query`、`version?`、`operand_order?`、`unknown_terms_empty?`） |
| `GET /versions` / `GET /versions/{id}` | 版本列表 / 版本详情（含全集、词项、事件） |
| `POST /versions/commit` | 基于父版本提交 adds/deletes，产生不可变新版本 |
| `GET /versions/{id}/terms/{term}` | 读取单个 posting |
| `GET /diagnostics/traces[?limit=]` / `GET /diagnostics/traces/{request_id}` | 执行轨迹 |

失败响应统一为：

```json
{"ok": false, "request_id": "req_...", "error": {"category": "parse_error", "message": "..."}}
```

失败类别（`error.category`，与 HTTP 状态码对应）：

| category | 状态码 | 含义 |
| --- | --- | --- |
| `parse_error` | 400 | 查询文本词法/语法错误 |
| `unknown_term` | 404 | 引用了该版本从未索引过的词项（区别于“已知但 posting 为空”） |
| `version_not_found` | 404 | 版本不存在 |
| `version_conflict` | 409 | 如删除全集中不存在的文档 |
| `internal` | 500 | 其他内部错误 |

未知词项也可显式 `unknown_terms_empty=true` 按空 posting 继续求值，
此时结果 200 但 `uncertainties` 中单列“不确定结论”。

### 请求身份与可解释性

- 每个请求可由 `X-Request-ID` 头指定身份，缺省服务生成 `req_<hex>`；
  响应头与 body 都回带。
- `logs/service.log` 每行一条 JSON（时间、级别、request_id、事件、版本、
  关键步骤、统计、失败原因），可按 request_id 完整回放一次失败。
- 查询响应内联 `trace`：解析 → 全集加载 → 每个 AST 节点的输入规模、
  是否短路、访问了几个子节点、各阶段跳过块数；
  `failures`（失败原因）与 `uncertainties`（不确定结论）分区。

## 5. 真实使用示例

`cat AND NOT dog`（v2，稀疏减稠密）：

```bash
curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' -H 'X-Request-ID: demo-1' \
  -d '{"query":"cat AND NOT dog","version":2}'
```

返回（节选）：

```json
{"ok": true, "request_id": "demo-1", "version": 2,
 "result": {"count": 3, "doc_ids": [9, 17, 33]},
 "stats": {"blocks_skipped": 3, "ids_stepped": 72, "comparisons": 121,
           "block_probes": 49, "results_emitted": 3}}
```

提交新版本（新增 doc 41、删除 doc 1）：

```bash
curl -s -X POST http://127.0.0.1:8000/versions/commit \
  -H 'Content-Type: application/json' \
  -d '{"adds":[{"doc_id":41,"terms":["cat","newt"]}],"deletes":[1],"message":"v4"}'
```

## 6. 运行测试

```bash
.venv/bin/python -m pytest -q
```

实际输出结论：

```
144 passed, 1 warning in 1.69s
```

测试组成：

- `tests/test_spec.py`：AST 结构、优先级、括号、双重否定、9 种具体语法报错文本；
- `tests/test_blocked_postings.py`：分块不变量、40 组随机种子的跳跃安全性
  （落点恒为下界、永不越过目标）、30 组随机集合代数对照、空全集差集、结果唯一性；
- `tests/test_storage.py`：初始空版本、快照不可变、删除同步、重新加入、冲突/不存在分类；
- `tests/test_engine.py`：夹具全部查询逐条对照独立参考答案（v2/v3）、稀疏/稠密具体值、
  空全集 NOT、全否定、删除后查询、两种执行顺序一致、`intersect_scan` 顺序导致
  跳过块 3 vs 1、AND/OR 短路（断言未求值子节点数）、各类失败分类与不确定结论；
- `tests/test_api.py`：TestClient 端到端——具体结果、状态码 + category、
  request_id 关联、轨迹留存（含失败请求轨迹）、提交新版本后查询与旧版本不可变。

## 7. 设计说明：分块与跳跃

posting 按 `block_size`（默认 8）定长切块，每块记录上界（块内最大 ID）。
`skip_to(target)` 先用块上界整块跳过（统计 `blocks_skipped` / `block_probes`），
进入候选块后再逐 ID 前进（统计 `ids_stepped` / `comparisons`）。
交集用对称 zig-zag（两侧互相 `skip_to` 对方当前值）；
并集两路归并；差集扫描全集、被减侧跳跃追赶。统计随每个 AST 节点和整体结果返回，
核心求交不经过任何 SQL 集合运算。
