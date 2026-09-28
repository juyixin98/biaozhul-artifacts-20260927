# 湖表读时删除应用器（Lake Table Read-Time Deletion Applier）

纯后端服务。输入两类删除——**文件行号删除（position）** 与 **主键等值删除
（equality）**，在读取时把删除应用到 Parquet 数据文件，并支持物理重写
（compaction）。所有数据与外部参与方均为本地合成夹具，无生产账号、无网络依赖。

技术栈：Python 3.10+ / FastAPI / PyArrow / SQLite（标准库 sqlite3）。

---

## 1. 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 固定版本
pip install -e .                          # 安装本包（提供 deleter 命令）

pytest -q                                 # 67 个测试
python scripts/verify_service.py          # 28 项真实 HTTP 端到端检查
deleter demo --workspace demo_out         # 逐版本逐行依据演示
deleter serve --workspace .deleter_workspace --port 8000
```

依赖固定版本见 `requirements.txt`（与 `requirements.lock` 内容一致，后者是
`pip freeze` 全量锁定，含传递依赖的精确版本）。

---

## 2. 工程结构（模块边界与数据契约）

```
src/deleter/
├── errors.py                 # 统一错误契约（5 类，见 §6）
├── service.py                # 服务层：事务编排，不感知 HTTP
├── demo.py                   # 内置演示场景
├── __main__.py               # deleter serve / demo
├── adapters/                 # 【格式适配边界】
│   ├── parquet_format.py     #   Arrow/Parquet 读写、类型校验、内容指纹
│   ├── ingestion.py          #   inline / inbox 来源归一化、行配额
│   └── lineage.py            #   行血缘侧录（insert_seq 与父行坐标）
├── kernel/                   # 【执行内核边界】纯函数，无 IO
│   ├── models.py             #   FileData/DeleteOp/ScanReport/原因码
│   ├── predicates.py         #   NULL 感知键比较（SQL 三值逻辑）
│   └── executor.py           #   逐行判定 + 操作级评估
├── metadata/
│   └── store.py              # 【元数据事务边界】SQLite：表/文件版本/删除/序列号
├── observability/
│   └── run_logger.py         # run_id 运行日志（可重放）
└── api/                      # 【验证接口边界】FastAPI + Pydantic
    ├── schemas.py
    └── app.py

tests/
├── conftest.py               # 夹具（临时工作区 + TestClient + 合成数据）
├── fixtures/golden_canonical.json   # 人工推导的逐版本黄金答案
├── oracle/reference.py       # 独立参考预言机（不 import 任何被测代码）
├── scenarios/                # 语义场景、错误分类、查询语义、日志
└── test_adapters.py
scripts/verify_service.py     # 不依赖 pytest 的真实 HTTP 端到端验证
```

模块间数据流：`api(schema 校验) → service(事务) → {metadata(SQLite),
adapters(Parquet/血缘)} → kernel(纯函数评估) → ScanReport(逐行结论)`。
内核不允许 import adapters/metadata/api；错误只用 `errors.py` 的类别跨层传递。

---

## 3. 核心语义（边界语义，务必先读）

### 3.1 序列号与可见性

* 每次**载入（插入）**与每次**删除注册**在同一 SQLite 事务中分配一个全局
  单调序列号 `seq`。重写不发号；被前置校验拒绝的请求不发号。
* 每行带 `insert_seq`（它进入本表的序列号，重写时原样延续）。
* 等值删除命中条件（**两者都满足才删除**）：
  1. 键值按 §3.3 的 NULL 规则相等；
  2. `row.insert_seq <= delete.seq` —— 删除发生时该行已经存在。

因此**先删后插是安全的**：后插入的同键行天然在删除的序列可见范围之外，
删除不能"穿越时间"命中它。重复键行各自独立判断（同一文件或跨文件）。

### 3.2 位置删除绑定"文件内容身份"，不是行号数字

* 位置删除注册时绑定目标文件的**当前版本号**与 0 基行号。
* 物理重写产生新文件版本（新 file_id，旧 file/version 标记 superseded）。
  旧位置删除只在"文件当前版本 == 绑定版本"时有效。
* **重写后绝不复用旧行号**：即使新文件同一行号恰好还有另一行，旧删除也
  不会作用于它；必须对新内容重新下删除。
* 失效操作在每次扫描时给出细分状态：
  - `stale_row_already_removed`：绑定行在使文件退出 live 的重写之前
    就已经被删除（未作为幸存内容进入任何重写产物）；
  - `stale_file_rewritten`：绑定行曾随某次重写幸存、内容身份已更替；
  - `stale_out_of_range`：绑定的旧版本本来就没有该行号。

行号采用 **0 基**。幸存行重写后在新文件中保持其相对行号（前缀压缩，
不重排），血缘侧录逐行记录父坐标 `[file_id, version, row_number]`。

### 3.3 NULL 键比较规则（SQL 三值逻辑的等值侧）

1. **谓词含 NULL**（`WHERE k = NULL`）：恒为 UNKNOWN，**不删除任何行**，
   操作状态 `applied_zero_rows`。
2. **行键含 NULL**：不与任何谓词相等，包括同样写 NULL 的谓词
   （`NULL = NULL` 是 UNKNOWN，不是 TRUE）。
3. 复合键：所有组件都非 NULL 且逐组件相等才命中。
4. 不做隐式跨类型转换：`True != 1`（bool 不视作 int）；int/float 同为
   数值时按数值比较。

保留行的原因码（互斥，按特异性排序）：
`in_scope_insert`（有键值相等的谓词但行晚于它插入）> `null_row_key_blocked`
（行键 NULL，对所有谓词 UNKNOWN）> `null_key_blocked`（行键非 NULL 且
**全部**谓词都是 NULL）> `no_match`。

### 3.4 过滤与列裁剪不改变删除语义

查询总是**先应用删除、再做过滤/投影**。因此：
* 裁掉主键列再查询，被删行不会复活；
* 在谓词列上做过滤，不能把"本应已删除"的行过滤回来；
* 过滤谓词自身同样遵守三值逻辑：`eq null` 不匹配任何行，`neq` 不匹配
  NULL 行，`is_null` / `not_null` 显式处理 NULL。

### 3.5 幂等与冲突

* 同一 `delete_id` 携带**完全相同**的删除规格重复提交 → 幂等，返回原
  `seq`，不发新号。
* 同一 `delete_id` 但规格不同 → `409 state_conflict`。
* 重写结果为空（所有行已删）→ 拒绝产生空文件版本，`409`。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tables` | 建表：`table_id, columns{列:类型}, key[主键列]` |
| GET | `/tables` / `/tables/{id}` | 列表 / 描述（含 live 文件、seq 水位） |
| POST | `/tables/{id}/load` | 载入不可变文件：`file_id` + inline/inbox 来源 |
| POST | `/tables/{id}/deletes` | 批量注册删除（按数组顺序发号），立即回评 |
| POST | `/tables/{id}/rewrite` | 物理重写：`file_ids[]` → `new_file_id` |
| POST | `/tables/{id}/query` | 读时查询：`filters[]`、`columns[]` |
| GET | `/tables/{id}/snapshot` | 完整逐行结论 + 操作评估 + 内核 trace |
| GET | `/tables/{id}/rows/{fid}/{rn}` | 单行判定依据 |
| GET | `/tables/{id}/files` / `/deletes` | 元数据 |
| GET | `/runs` / `/runs/{run_id}` | 运行日志索引 / 完整重放记录 |

成功响应统一包一层 `{"run_id": ..., "result": ...}`；错误响应为
`{"error": {"category", "message", "details"}}`，并带 `X-Run-Id` 头。
支持的列类型：`int64 / int32 / string / bool / float64`。请求体形状错误
由 Pydantic 返回 422；语义错误由服务返回下述 400/404/409/413/500。

完整可执行示例见 `scripts/verify_service.py`。

---

## 5. 可复核输出

### 5.1 三级参考答案，互为独立来源

1. **人工黄金答案** `tests/fixtures/golden_canonical.json`：一张多文件
   表上 10 个操作步骤，逐步给出 live 版本、删除集合，最终逐行给出
   action/reason/归因 delete_id/insert_seq，以及手工的序列号分配表。
2. **独立参考预言机** `tests/oracle/reference.py`：用另一种写法独立
   维护文件版本、序列号、三值逻辑与重写血缘，**不 import 任何
   `deleter.*` 代码**；原因码字符串也独立（`R_*`），与内核常量的
   等价关系在测试里手写声明，防止"常量复用导致两边一起错"。
3. **被测内核** 的 `ScanReport`。

`test_golden.py` 做三方对照；`test_property_vs_oracle.py` 用 30 个随机
种子生成操作流（多文件/重复键/NULL/先删后插/位置删除/重写链），逐行
比对服务与预言机的 action、reason、归因操作、seq、值。

### 5.2 测试不只检查"接口能调用"

每个场景断言**具体行的具体结果与失败类别**，例如：
* 重写后旧位置删除状态必须是 `stale_row_already_removed`，新文件 rn=2
  上的另一行必须 `keep`；
* 先删 id=5 再插两个 id=5，两行都必须 `keep / in_scope_insert` 且归因
  到原来的删除；
* 谓词 `id=NULL` 状态必须 `applied_zero_rows`，NULL 行原因为
  `null_row_key_blocked`；
* 类型不符 / 越界行号 / 重复 ID 异规格分别断言 400/400/409 及 details。

### 5.3 运行日志（可重放问题）

每个被处理的操作（含失败）生成 `run_id`，落盘
`<workspace>/runs/<run_id>.json`：请求摘要、错误类别与 details、
`kernel_trace`（逐行 verdict、越界保留证据、失效分类的判断理由）、
`state_after`（seq 水位、live 文件版本、删除清单）、耗时；
`runs/index.jsonl` 按编号检索。`GET /runs/{run_id}` 可完整取回。

---

## 6. 错误分类（输入错误 / 状态冲突 / 资源耗尽 / 计算失败 可区分）

| category | HTTP | 含义 | 触发示例 |
|---|---|---|---|
| `input_error` | 400 | 请求本身不合法 | 类型不符、越界行号、键列缺失/多余、未知格式、inbox 路径逃逸 |
| `not_found` | 404 | 资源不存在 | 表/文件/run_id 不存在 |
| `state_conflict` | 409 | 请求合法但与状态矛盾 | 表或 file_id 已存在、对 superseded 文件下位置删除、delete_id 同 ID 异规格、空重写 |
| `resource_limit` | 413 | 超过本地配额 | 单次载入行数超过 `max_rows_per_load`（默认 200000） |
| `compute_failure` | 500 | 内核/底层计算失败 | Parquet 字节损坏无法读取、血缘与数据行数不一致 |

请求 JSON 结构不合法（缺字段/类型错）由 Pydantic 返回 **422**，这是
"连领域校验都未进入"的形状错误，与 400 语义错误刻意区分。

---

## 7. 存储布局

```
<workspace>/
├── data/
│   ├── meta.sqlite3           # 表/文件版本/删除/序列号（WAL 事务）
│   ├── lineage/<table>/<file>.v<ver>.json
│   └── tables/<table>/<file>.v<ver>.parquet
├── inbox/                     # 唯一允许的本地文件来源目录
└── runs/<run_id>.json + index.jsonl
```

Parquet 写临时文件后原子改名；序列号分配与元数据写入同一 SQLite 事务
（`BEGIN IMMEDIATE` + 进程内锁串行化），失败回滚不留半成品。

---

## 8. 边界与未执行的检查（明确单列，不宣称已通过）

本服务是**合成教学/验证实现**，以下是刻意不做或未验证的内容，不应被
当作已具备的能力：

* **未做并发压力验证**：写路径用进程内锁串行化，只验证了单进程正确性，
  没有跑多线程/多进程并发测试，也没有性能/规模基准。
* **未做崩溃恢复测试**：Parquet 与 SQLite 的原子性依赖本地文件系统
  rename 与 SQLite WAL；没有注入"写到一半掉电"的故障，恢复行为未验证。
* **无鉴权 / 多租户 / 网络来源**：inbox 仅限工作区本地目录，无任何
  认证授权；不适合直接暴露到不可信网络。
* **类型覆盖有限**：仅 int64/int32/string/bool/float64；没有 decimal、
  date/timestamp、binary、嵌套/列表类型。
* **删除谓词仅支持全主键等值**：不支持非主键列删除、范围/IN/模糊删除；
  查询过滤（query filters）与删除谓词是两套独立能力。
* **重写是手动触发的整文件幸存行压缩**：不自动 compaction，不处理
  分区分桶、排序优化、统计信息下推。
* **序列号是单表单计数器**：无跨表事务；一次删除批内多操作按数组顺序
  各占一号，不提供批量单快照语义（它们仍是同一注册事务提交）。
* **未验证 Windows/macOS**：只在 Linux + Python 3.12 上执行过测试。

---

## 9. 测试与验证清单

| 命令 | 内容 | 结果 |
|---|---|---|
| `pytest -q` | 67 个测试（黄金三方对照、30 随机流属性测试、错误分类、查询语义、位置身份、适配、日志） | 全部通过 |
| `python scripts/verify_service.py` | 真实 uvicorn 进程上的 28 项 HTTP 断言 | 全部通过 |
| `deleter demo` | 逐版本打印每行保留/删除依据并落盘 snapshot | 手工可读 |

黄金场景覆盖题目要求的四类用例：**重写文件**（fA→fC 后旧行号失效）、
**先删后插**（id=5）、**重复键**（f1 两个 id=2、f3 两个 id=5）、
**跨文件删除**（等值删除 id=2 同时命中 fA 与 fB）。
