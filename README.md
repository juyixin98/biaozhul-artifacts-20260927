# 持久 Posting 列表布尔查询服务（AND / OR / NOT / 短路 / 跳跃块）

在**显式版本化文档全集**上，对持久化到 SQLite 的分块 posting 列表执行
布尔查询（AND / OR / NOT），支持短路求值、跳跃块安全跳过与完整执行诊断。

- 技术栈：Python 3.12 · FastAPI · SQLite（无外部服务、无生产账号依赖）
- 所有数据均为本地合成夹具（`sample_data/sample.json`）

---

## 1. 快速开始

```bash
# 1) 建虚拟环境并安装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 播种本地合成样例语料（两版本：8 篇初始文档；v2 加 1 篇、删 1 篇）
.venv/bin/python scripts/seed.py

# 3) 启动服务
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
# 或：.venv/bin/python -m app.main
```

启动后可用的环境变量（均有默认值，见 `app/config.py`）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `POSTING_DB_PATH` | `data/postings.db` | SQLite 文件路径 |
| `POSTING_LOG_DIR` | `logs/` | JSONL 结构化日志目录 |
| `POSTING_BLOCK_SIZE` | `8` | posting 列表跳跃块大小 |
| `POSTING_HOST` / `POSTING_PORT` | `127.0.0.1` / `8000` | 监听地址 |

冒烟（另开终端）：

```bash
curl -s "http://127.0.0.1:8000/health"
curl -s "http://127.0.0.1:8000/query?expr=alpha%20AND%20NOT%20common&version=2"
curl -s "http://127.0.0.1:8000/explain?expr=alpha%20AND%20beta%20AND%20gamma&version=2"
```

---

## 2. 运行测试（真实命令与输出结论）

```bash
.venv/bin/python -m pytest -q
```

本仓库最近一次运行的真实结论：

```
135 passed, 1 warning in 4.21s
```

测试分两层：

- `tests/unit/`：分词与查询规范、分块/游标/集合代数、版本存储、查询引擎、
  随机属性测试、诊断，以及两条**架构守卫**（见下）。
- `tests/integration/test_api.py`：FastAPI + SQLite 真实 HTTP 端到端。

随机属性测试（`test_randomized_equivalence.py`）在开发中真实抓到过两个 bug：
OR 仅按基数判断短路（长度相等但非全集时错误短路）、空全集快照版本被误判不存在；
二者均已修复并由测试回归保护。

### 参考答案独立性（关键约束）

期望值由 `tests/_oracle.py` 用纯 Python `set` 集合代数**独立**计算，
该文件不导入任何 `app.*` 模块，并有静态测试强制：

```bash
.venv/bin/python -m pytest tests/unit/test_oracle_independence.py -q
# 2 passed
```

另有守卫测试确保求交/并/补由游标核心完成，而不是用 SQL 集合查询代替：

```bash
.venv/bin/python -m pytest tests/unit/test_no_sql_setops.py -q
# 2 passed
```

---

## 3. 模块划分（多模块，各自承担实际工作）

```
app/
├── config.py                 # 配置（环境变量）
├── text/                     # 文本规范
│   ├── tokenize.py           #   分词（文档与 term 同一套规则）
│   └── spec.py               #   布尔表达式词法/语法/AST/失败类别
├── index/
│   └── posting.py            # 算法索引：分块 posting、跳跃游标、
│                             #   intersect/union/difference + 执行统计
├── storage/
│   ├── encoding.py           # 块的二进制编解码（BLOB）
│   └── version_store.py      # SQLite：文档/事件日志/版本化全集快照/
│                             #   持久跳跃块/请求 trace
├── query/
│   ├── planner.py            # AST → 逻辑计划（可见全集包装、顺序策略）
│   ├── executor.py           # 执行器：AND/OR/NOT、短路、统计、步骤
│   └── engine.py             # 引擎外观：统一结果/失败/不确定性/explain
├── diagnostics/
│   ├── logging_setup.py      # JSONL 结构化日志
│   ├── tracer.py             # 请求身份
│   └── errors.py             # 失败类别 → HTTP 状态、错误信封
├── api/
│   ├── routes.py             # HTTP 路由
│   └── service_error.py      # API 异常
└── main.py                   # 应用工厂、请求身份中间件、异常处理
tests/                        # 独立单元 + 集成测试 + 独立 oracle
sample_data/sample.json       # 本地合成语料
scripts/seed.py               # 播种脚本
```

没有硬编码演示：查询结果全部来自“持久块 → 游标集合运算 → 可见性过滤”。

---

## 4. 核心语义与不变量

1. **NOT 相对于显式版本化全集**。每个版本都物化一份有限的可见文档全集
   （`universe_members`），NOT = `universe − 子结果`。不存在任何
   “非 term 即所有非负整数”的补集入口；空全集（版本 0）上 NOT 结果为空。
2. **跳跃块上界只用于安全跳过**。游标 `advance_to(t)` 只跳过
   `block.upper < t` 的整块；`upper == t` 的块必须进入块内检查。
   不改变结果集合，只减少检查量，跳过的块数与块内文档数都会被统计。
3. **删除只同步全集可见性**。删除文档时不重写 posting/块；所有计划根节点
   与该版本可见全集求交（`visible_filter`），因此删除即时生效，块上界依然安全。
4. **每个结果 ID 唯一**。posting 构造强制严格递增无重复；并集相遇只收一次；
   根计划与可见全集求交；执行器末尾还有唯一性断言兜底。
5. **执行统计来自游标核心**。比较次数、逐文档检查数、整块跳过数、
   跳过块内文档数、`next/advance` 调用次数均由 `CursorStats` 累加，
   不是用数据库 COUNT/INTERSECT 查询代替求交（有守卫测试）。

### 短路求值

- AND：成对折叠，任一中间结果为空立即终止，剩余子树不求值
  （响应含 `short_circuited` 与 `skipped_nodes`）。
- OR：仅当中间结果**集合上真正覆盖**显式全集（不是只看长度）才短路。

### 执行顺序

`rare_first`（默认，稀疏驱动/全集吸收）、`textual`（从左到右）、
`reverse`（从右到左）。三种顺序结果必须逐 ID 一致，统计可以不同；
`GET /explain` 一次跑三种顺序并分别给出跳过块汇总。

---

## 5. HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 最新版本、块大小、库路径 |
| `GET /spec` | 文本规范语法、term 规则、失败类别清单 |
| `POST /versions/commit` | 提交 `{adds:{id:text}, deletes:[id]}`，生成新版本 |
| `GET /versions` | 版本列表与各版本全集大小 |
| `GET /terms?prefix=&limit=` | term 列举 |
| `GET /query?expr=&version=&order=` | 布尔查询（核心） |
| `GET /explain?expr=&version=` | 三执行顺序一致性 + 跳过块对比 + 步骤明细 |
| `GET /diagnostics/requests` | 最近请求 |
| `GET /diagnostics/requests/{request_id}` | 单请求完整 trace（步骤/统计/失败） |

查询响应（成功）关键字段：

```json
{
  "ok": true,
  "request_id": "…",
  "expression": "alpha AND NOT common",
  "version": 2,
  "order": "rare_first",
  "result": [1, 2, 3, 9],
  "count": 4,
  "short_circuited": false,
  "skipped_nodes": [],
  "stats": { "comparisons": 6, "docs_examined": 13,
             "blocks_skipped": 0, "docs_skipped_in_blocks": 0,
             "next_calls": 13, "advance_calls": 1 },
  "warnings": [],
  "uncertainty": [],
  "steps": [ {"op": "term_load", "label": "term:alpha",
              "detail": "…块上界=[…]", "stats": {…}} ],
  "ast": { "type": "and", "children": [ … 带字符 span … ] }
}
```

失败响应（原因与不确定结论**单列**，不与结果混排）：

```json
{
  "ok": false,
  "request_id": "…",
  "error": { "category": "spec_error", "message": "AND 之后缺少操作数",
             "position": 9 },
  "uncertainty": []
}
```

失败 `category`：`spec_error`（含字符位置，子类见 `/spec`）、
`validation_error`、`order_error`、`version_not_found`(404)、
`storage_error`、`internal_error`、`trace_not_found`(404)。

未知 term 不是错误：按空列表参与集合代数，同时在
`warnings` / `uncertainty` 中显式标注（“不确定结论”单列）。

---

## 6. 失败可复现 / 可解释

- 每个请求都有 `request_id`：可用请求头 `X-Request-ID` 指定，否则自动生成；
  响应头与所有记录都带同一 ID。
- `steps` 展示关键处理位置与动作：`term_load`（含块数与块上界）、
  `intersect` / `union`（比较数、整块跳过数、跳过块内文档数）、
  `universe_difference`（全集 − 子结果）、`visible_filter`（删除过滤）。
- 失败时 `error.category` + `message` + 字符 `position` 单列；
  trace 持久化在 SQLite `request_traces`，事件流写入 `logs/service.jsonl`。
- 复现路径：取响应头 `X-Request-ID` →
  `GET /diagnostics/requests/{id}` 查看版本、步骤、统计与失败原因；
  或在 JSONL 中按 `request_id` 过滤。

示例：

```bash
curl -s -H "X-Request-ID: demo" \
  "http://127.0.0.1:8000/query?expr=alpha%20AND&version=2"
# HTTP 400, error.category = spec_error, position = 9
curl -s "http://127.0.0.1:8000/diagnostics/requests/demo"
```
