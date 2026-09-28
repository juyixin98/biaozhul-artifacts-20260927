# 复合键 MERGE 决策器（Composite-key MERGE Decision Engine）

批量源表 → 目标快照的**受限 MERGE** 后端：匹配更新（matched UPDATE）、未匹配插入
（unmatched INSERT）、未匹配条件删除（conditional DELETE）。技术栈 Python /
FastAPI / PyArrow / SQLite，全部数据为本地合成夹具，无生产账号与外部依赖。

---

## 1. 本地验证命令

```bash
# 1) 建虚拟环境并安装依赖（已在 Python 3.12 + 下述固定版本验证）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 跑全部测试（100 个），含第三阶段捕获用例与独立 oracle 交叉核对
.venv/bin/python -m pytest -q
#    只跑第三阶段捕获用例：
.venv/bin/python -m pytest -m capture -v
#    带随机种子复现某个 property 用例（seed 即参数）：
.venv/bin/python -m pytest tests/test_oracle_crosscheck.py -k "seed_23" -v

# 3) 端到端手工核验演示（打印动作集合、前后快照、run_id 与日志路径）
PYTHONPATH=src .venv/bin/python scripts/demo.py

# 4) 启动 HTTP 服务（本地；--allow-fault-injection 仅用于本地故障演练）
mkdir -p var
PYTHONPATH=src .venv/bin/python -m merge_engine.cli \
    --db var/merge.db --journal var/journal --allow-fault-injection --port 8080
#    健康检查：  curl -s 127.0.0.1:8080/healthz
#    OpenAPI：   http://127.0.0.1:8080/docs
```

### curl 最小闭环

```bash
curl -s -X POST 127.0.0.1:8080/v1/merge/dry-run -H 'content-type: application/json' -d '{
  "source": {"format":"records","records":[{"k1":"a","k2":1,"v":2}]},
  "config": {"target_table":"demo","key_columns":["k1","k2"]}}'

curl -s -X POST 127.0.0.1:8080/v1/merge -H 'content-type: application/json' -d '{
  "source": {"format":"records","records":[{"k1":"a","k2":1,"v":2}]},
  "config": {"target_table":"demo","key_columns":["k1","k2"]}}'

curl -s 127.0.0.1:8080/v1/targets/demo/rows
```

### 预期判断方式

- **成功**：`status=COMMITTED`，`counts` 给出三类写动作数；目标表逐行符合预期；
  `__merge_actions` 表中有与计划完全一致的审计行。
- **dry-run**：`status=PLANNED`，返回完整 `plan.actions`，目标表内容不变。
- **被拒绝**：`status=REJECTED`，`error.category/code` 命中下表，目标表不变。
- **提交失败**：`status=FAILED`，目标表与操作前快照**逐行相等**（演示脚本会断言）。
- **日志重放**：打开 `var/journal/<run_id>.jsonl`，从 `run_started` 到
  `run_committed/run_rejected/commit_failed` 可看到每一阶段的中间状态与 reason；
  失败后去掉 `fault_point` 用相同请求重跑，两次的 `snapshot_fingerprint` 相同。

---

## 2. 算法假设（语义契约）

### 2.1 匹配与复合键

- 键是 `key_columns` 的**有序元组**；匹配在“操作前目标快照”的冻结索引上进行。
- 同一批次内，**前面源行决定插入的键不会被后面源行匹配到**（快照在决策开始时
  冻结，决策与提交是两个阶段，结构上杜绝边插边匹配）。
- 删除集合 = 操作前快照中**未被任何源行匹配**的目标行，再施加 `delete_when`。
  同批新插入的行从不在删除扫描范围内。

### 2.2 NULL 相等策略（`null_equality`，优先级在配置解析时确定）

| 策略 | 键匹配 | 源键含 NULL | 目标键含 NULL |
|---|---|---|---|
| `SQL`（默认） | SQL 三值逻辑，NULL≠NULL | **拒绝** `KEY_NULL_REJECTED`（一次性列出全部行） | 永不匹配，也互不算重复 |
| `DISTINCT` | `IS NOT DISTINCT FROM`，NULL==NULL | 可匹配，NULL 键作为普通值 | 两个 NULL 键互为重复 → `TARGET_DUPLICATE_KEY` |

NULL 策略只作用于**键匹配**。条件表达式内部一律按 SQL 三值逻辑求值（见 2.4）。

### 2.3 源/目标重复键

- **源内同键多行**：在接触目标之前整体扫描、一次性收集**全部**冲突键及行号后拒绝
  （`SOURCE_DUPLICATE_KEY`，400）。报告顺序按首次出现位置排序，与行序无关——
  `test_source_duplicate_rejection_independent_of_order` 对多种排列做了断言。
- **目标同键多行**：目标是业务脏状态，内核拒绝猜测更新哪一行
  （`TARGET_DUPLICATE_KEY`，409），返回冲突键与全部 rowid。目标表刻意**不**建
  UNIQUE 约束，以免 SQLite 替我们随机拒绝一行。

### 2.4 条件与规则优先级

- 三类条件：`update_when`（可引用 source/target）、`insert_when`（仅 source）、
  `delete_when`（仅 target）。条件是显式 JSON 谓词树（`all/any/not` +
  `eq/ne/gt/gte/lt/lte/is_null/is_not_null`），**不使用 eval**。
- 三值逻辑：比较任一操作数为 NULL → NULL；只有 **TRUE** 触发写动作；
  NULL 与 FALSE 在动作上同为“不写”，但 **reason 明确区分**
  （`*_COND_NULL` vs `*_COND_FALSE`），便于重放判断。
- 无条件时：update/insert 恒成立；`delete_unmatched=true` 必须显式给出
  `delete_when`（拒绝“删全部”的歧义配置）。
- UPDATE 投影：`after` 覆盖“本批列 ∪ 目标既有列”；本批未携带的既有列显式置 NULL
  （稀疏批语义：缺列补 NULL，已在适配层对本批列并集完成）。

### 2.5 先验证再原子提交

固定阶段顺序（错误优先级）：

```
适配读源 → 目标结构引导 → 配置解析 → 源资源上限 → 装操作前快照
        → planner 决策（源重复 → 源NULL键 → 目标重复 → 逐行）
        → 计划资源/序列化验证
        → 单个 BEGIN IMMEDIATE 事务提交（actions 审计先行 + 目标改动 + runs 元数据）
```

提交在一个 SQLite 事务内完成；任何异常都 `ROLLBACK`。三个故障注入点用于验证：
`after_actions`（行已改未提交，500/COMMIT_FAILED）、`before_commit`（ENOSPC，
507/DISK_FULL）、`commit_raises`（commit 自身磁盘 I/O，500/COMMIT_FAILED）。

### 2.6 四类失败的区分

| category | HTTP | code 示例 | 含义 |
|---|---|---|---|
| `INPUT_ERROR` | 400 | `SOURCE_FORMAT_ERROR` / `SCHEMA_MISMATCH` / `SOURCE_DUPLICATE_KEY` / `KEY_NULL_REJECTED` / `CONFIG_INVALID` | 输入不合法，同输入重试无意义 |
| `STATE_CONFLICT` | 409 | `TARGET_DUPLICATE_KEY` | 输入合法但目标快照状态使契约无法满足 |
| `RESOURCE_EXHAUSTED` | 507 | `PLAN_TOO_LARGE` / `DISK_FULL` | 行数/动作数/字节数超限或容量耗尽 |
| `COMPUTATION_FAILURE` | 500 | `COMMIT_FAILED` / `CONDITION_EVALUATION_FAILED` | 执行/提交期系统失败 |

> 注：请求体本身不符合 Pydantic 模型是框架层 422（如格式字段拼错）；
> 请求体可解析但违反业务规则时按上表返回 400/409/507/500。

---

## 3. 模块关系与数据/错误契约

```
HTTP 请求
   │  api.py（FastAPI；请求模型 + 四类错误→HTTP 映射；查询接口）
   ▼
engine.py（MergeEngine 编排：阶段顺序、run_id、journal、RunResult）
   │
   ├─ adapter.py    records/NDJSON/Parquet ──► SourceBatch(SourceRow[])
   │                                 错误：SOURCE_FORMAT_ERROR（400）
   ├─ config.py     原始 dict ──► MergeSpec（键/NULL策略/条件树/资源上限）
   │                                 错误：CONFIG_INVALID（400）
   ├─ snapshot.py   操作前快照 ──► TargetSnapshot（冻结索引 + 指纹）
   │                                 错误：TARGET_DUPLICATE_KEY（409）
   ├─ planner.py    (SourceBatch, MergeSpec, pre-image) ──► MergePlan(Action[])
   │                  纯决策、不写库；源重复/NULL键在此拒绝
   ├─ validator.py  源与计划的行数/动作数/字节数上限、JSON 可序列化
   │                                 错误：PLAN_TOO_LARGE（507）
   ├─ store.py      SQLite：引导/装快照/单事务 apply_plan/运行与动作元数据
   │                  数据列经 valuecodec 类型保真编解码
   │                                 错误：COMMIT_FAILED（500）/ DISK_FULL（507）
   ├─ valuecodec.py bool/int/float/str/bytes/NULL 的标记化 BLOB 编解码
   └─ journal.py    每 run 一个 JSONL：阶段事件 + 中间状态 + reason（可重放）
```

- **数据契约**（`contracts.py`）：`SourceRow / TargetRow / Action / MergePlan /
  RunResult` 全部 JSON 可序列化；动作带 `before/after/key/source_rownum/
  target_rowid/reason`，计划带操作前快照 `snapshot_fingerprint` 与 rowid 列表。
- **错误契约**（`errors.py`）：唯一异常体系 `MergeError`，每个错误带稳定
  `category`、`code`、`details`，跨层不吞错、不改变类别。
- **决策/提交分离**：`MergePlan` 是纯数据，可以 dry-run 审核、记录、重放；
  提交层只消费计划，不再做匹配判断。

测试侧：`tests/oracle.py` 是**独立命令式参考实现**（不 import 任何被测决策代码），
40 个随机用例与排列不变性用例把内核动作集合逐条对照 oracle；第三阶段规定场景的
期望值在测试中手工推导并注释，不由被测核心生成。

---

## 4. 元数据与日志

- `__merge_runs`：run_id、时间、dry-run、状态（`COMMITTED/PLANNED/REJECTED/
  FAILED`）、快照指纹、计数、错误 JSON。被拒绝与失败的运行也登记。
- `__merge_actions`：仅随成功事务提交；逐动作审计（类型、键、前后镜像、理由）。
  提交失败时随事务一起回滚——“有审计行”当且仅当“改动已提交”。
- `var/journal/<run_id>.jsonl`：每行一个事件（fsync 落盘），含 run_id、时间戳、
  phase、event、reason 与关键中间状态（源列/字节、快照 rowid、指纹、动作摘要、
  资源统计、故障点、错误对象）。

接口：`POST /v1/merge/dry-run`、`POST /v1/merge`、`GET /v1/runs/{id}`、
`GET /v1/runs/{id}/actions`、`GET /v1/targets/{table}/rows`、`GET /healthz`。

---

## 5. 依赖版本（requirements.txt，已验证）

| 包 | 版本 | 用途 |
|---|---|---|
| Python | 3.12.3 | 运行时（要求 ≥3.11） |
| fastapi | 0.141.1 | 验证接口 |
| starlette | 1.7.0 | FastAPI 底层（TestClient） |
| uvicorn | 0.54.0 | 本地 ASGI 服务 |
| pydantic | 2.13.5 | 请求模型 |
| pyarrow | 21.0.0 | Parquet 适配 |
| httpx | 0.28.1 | TestClient 传输 |
| pytest | 8.4.2 | 测试框架 |
| sqlite3 | 标准库 3.x（WAL） | 存储与事务 |

无其他外部服务；`fault_point` 是本地合成测试设施，HTTP 默认关闭
（`--allow-fault-injection` 或工厂参数开启）。

## 6. 测试覆盖清单（截至当前，全部通过：100 个）

- `test_adapter.py`：三格式、稀疏补 NULL、解析定位、非标量拒绝（6）
- `test_conditions.py`：三值逻辑、全序比较、侧/列契约（7）
- `test_capture_contracts.py`：复合键全动作、目标重复、更新影响条件、
  源重复（全量+行序无关）、SQL/DISTINCT NULL 策略（10）
- `test_snapshot_isolation.py`：匹配只见操作前快照、删除集合冻结、指纹（3）
- `test_atomic_commit.py`：三注入点无部分更新、失败重放、dry-run（含引用目标列）、
  审计表随事务提交（7）
- `test_limits_and_priority.py`：资源耗尽分类与错误优先级（7）
- `test_journal_replay.py`：run_id/中间状态/reason 重放（4）
- `test_api.py`：HTTP 四类状态映射、dry-run/commit、故障注入、查询接口（10）
- `test_oracle_crosscheck.py`：独立 oracle 40 随机用例 + 排列不变 + 三格式一致（42）
- `test_value_fidelity.py`：bool/int/float/str/bytes/NULL 存储类型保真（4）

**未运行/未通过**：无。100 个用例在当前环境（Python 3.12.3 / SQLite 3.45.1）
全部通过。Parquet 路径依赖 PyArrow（已在 requirements 固定）；若在无 PyArrow 的
环境运行，相关用例会在导入/读取处显式失败，不会被静默跳过。

## 7. 存储类型保真说明

SQLite 默认会把 Python `bool` 存成 `INTEGER 0/1`，列亲和性也可能改写文本/字节串。
为保证决策与读回看到的是源数据的**原始 Python 类型**，所有目标表数据列声明为
自定义类型 `MERGEVAL`：写入时由 `valuecodec.encode` 编码成带 1 字节类型标记的
BLOB，读出时经 `PARSE_DECLTYPES` 转换器精确还原（bool 仍是 bool、大整数、浮点、
UTF-8 文本、原始字节、NULL 全部逐值往返）。该编解码只作用于目标数据列：

- 键比较、条件求值都在**解码后的 Python 值**上进行（不比编码字节）；
- 元数据表（`__merge_runs/__merge_actions`）与普通 SQL 参数不经过该编解码，
  避免进程级适配器污染其它绑定（这是实现中实测并修正过的一个陷阱）；
- API/日志中的 bytes 以 `{"__b64__": "..."}` 表达，与存储编码互不影响。
