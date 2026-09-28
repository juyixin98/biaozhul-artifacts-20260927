# 复合键 MERGE 决策器 (Composite-Key MERGE Decision Engine)

受限的批量「源表 → 目标快照」MERGE 后端：**匹配更新 (matched UPDATE)、未匹配插入
(not matched INSERT)、条件删除 (conditional DELETE)**。全部决策先验证、再在单个
SQLite 事务内原子提交。技术栈：Python 3.12 · FastAPI · PyArrow · SQLite（全部本地
合成夹具，无生产账号、无外部业务数据）。

---

## 1. 本地验证命令

```bash
# 1) 建立虚拟环境并安装锁定版本（仅首次）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 运行全部测试（45 项），输出每条用例编号与失败类别
.venv/bin/python -m pytest -v

# 3) 端到端本地演示（无需启动服务，走进程内 service）
.venv/bin/python scripts/seed_demo.py data/merge-demo.db          # 合成目标表
.venv/bin/python scripts/run_demo.py examples/demo_merge.json \
    --db data/merge-demo.db --validate                            # 只出计划
.venv/bin/python scripts/run_demo.py examples/demo_merge.json \
    --db data/merge-demo.db                                       # 原子提交
.venv/bin/python scripts/run_demo.py examples/demo_merge.json \
    --db data/merge-demo.db --fail-commit                         # 注入提交失败

# 4) 用独立参考答案（oracle，不 import 被测核心）复核某次运行
.venv/bin/python scripts/replay.py --db data/merge-demo.db list
.venv/bin/python scripts/replay.py --db data/merge-demo.db verify <run_id>
.venv/bin/python scripts/replay.py --log logs/merge-runs.jsonl timeline <run_id>

# 5) HTTP 接口
MERGE_DB=data/merge-demo.db MERGE_PORT=8080 \
    .venv/bin/python -m merge_engine.api
#   POST /v1/merge/validate   只验证、返回动作计划，不改数据
#   POST /v1/merge            验证并原子提交（body.options.failpoint_commit=true 注入故障）
#   GET  /v1/runs             最近运行
#   GET  /v1/runs/{id}        /actions /traces /snapshot
#   GET  /healthz
```

### 预期判断方式

- **pytest**：全部 `PASSED`。每条用例编号为 `TC-PLAN-*`（决策核心，11）、
  `TC-ATOM-* / TC-ERR-*`（原子性与错误分类，13）、`TC-INV-*`（结构不变量，5）、
  `TC-API-* / TC-LOG-*`（接口与可重放日志，10）、`TC-EDGE-*`（边界行为，6）。
- **演示动作集合**（`examples/demo_merge.json`，4 条源行）应为：
  `cn/1 UPDATE balance=125`、`cn/2 DELETE`、`cn/4 INSERT balance=7`、
  `us/1 UPDATE balance=50`；计数 `{update:2, insert:1, delete:1}`。
- **故障注入**：提交前计数 5 行 → 报 `RESOURCE_EXHAUSTED/RESOURCE_COMMIT_FAILED`
  → 回滚后仍为 5 行（无部分更新）；`merge_runs.status='failed'` 但动作计划仍可查。
- **verify**：打印 `VERIFY OK: ... action set matches independent oracle`。

---

## 2. 算法假设（语义契约）

1. **匹配只看操作前目标快照。** 规划器 `planner.build_plan` 是纯函数，输入是已物化的
   目标行列表，完全不接触数据库。同批较早 INSERT 的行只存在于计划里，后续源行不可能
   匹配到它（`TC-INV-02/04/05` 行为断言 + `TC-INV-03` 对决策核心源码不含任何 SQL/连接
   构造的结构断言）。
2. **源内同键多行一律拒绝**（`INPUT_ERROR/INPUT_SOURCE_DUPLICATE_KEY`）。重复检测基于
   哈希分组并按「键令牌 + 原始位置」确定性排序输出，**不依赖偶然行序**（正反序产生同一
   冲突组，见 `TC-PLAN-04`、`TC-INV-01`）。
3. **目标重复键拒绝**（`STATE_CONFLICT/STATE_TARGET_DUPLICATE_KEY`）。MERGE 要求每个键
   至多一个匹配目标；即使目标表没建唯一约束也会在快照扫描时拦截。
4. **NULL 相等策略（仅作用于键匹配）**：
   - `NULLS_NOT_DISTINCT`（默认，等价 SQL `IS NOT DISTINCT FROM`）：两个 NULL 键分量相等。
   - `NULLS_DISTINCT`（等价 SQL `=` 三值逻辑）：含 NULL 的键永不匹配任何目标、含 NULL 的
     键彼此也不冲突，源行必走 NOT MATCHED。
   - WHEN 条件里的比较始终是 SQL 三值逻辑（`NULL = NULL → UNKNOWN`），不受键策略影响；
     UNKNOWN 不触发规则，并在判定轨迹里显式记为 `null`（`TC-PLAN-07`）。
5. **规则优先级明确**：WHEN 子句按列表顺序评估，只在同一侧（MATCHED / NOT MATCHED）内
   竞争，**第一个条件为真的规则触发**，其余不再评估为动作；没有任何规则触发时记为
   `UNPROCESSED`（不报错、不写）。
6. **全部先验证再原子提交。** 所有条件与赋值表达式在规划阶段对每一行求值；任何求值错误
   都聚合后以 `COMPUTATION_FAILURE` 拒绝，**此前不执行任何 SQL**。执行阶段在单个
   `BEGIN IMMEDIATE` 事务内按计划顺序应用，只 `commit` 一次；失败即 `rollback`，SQLite
   自身保证无部分更新，测试再用前后整表快照比对验证（`TC-ATOM-01/04`）。
7. **表达式方言**：SQL 风格条件（`=`、`<>`、`AND/OR/NOT`、`IS [NOT] NULL`、`''` 字符串、
   `||` 拼接）经词法翻译为受限 Python 表达式，再由**手写 AST 白名单遍历器**求值，永不
   `eval`；仅允许列引用（`S.col`/`T.col`/裸列）、算术/比较/布尔、以及白名单函数
   (`coalesce/nullif/upper/lower/length/substr/trim/abs/round/cast/case`)。裸列在两侧都
   存在时必须显式限定，否则按输入错误拒绝。NOT MATCHED 子句引用 `T.*` 同样拒绝。
8. **快照隔离复核**：执行 UPDATE/DELETE 前比对 rowid 行当前值与计划时快照；若有并发已
   提交的改动，以 `STATE_CONFLICT/STATE_SNAPSHOT_STALE` 中止（`TC-ATOM-05`）。
9. **键列保持**：UPDATE 不允许改键（`update_columns` 与键列交集即输入错误）；INSERT 的
   键列一律取自源行，非键列取赋值表达式。有数据库 DEFAULT 的 NOT NULL 列允许缺省。

---

## 3. 错误分类（四类可区分，附 HTTP 状态）

| category | code（节选） | 触发场景 | HTTP |
|---|---|---|---|
| `INPUT_ERROR` | `INPUT_INVALID_SPEC` | 规格结构/标识符/子句非法、表达式引用未知列或含禁用语法 | 400 |
| | `INPUT_SOURCE_SCHEMA` | 源格式/结构错误、列名重复 | 400 |
| | `INPUT_SOURCE_DUPLICATE_KEY` | 源内同键多行 | 400 |
| | `INPUT_UNSUPPORTED_VALUE` | Arrow/JSON 值类型不受支持 | 400 |
| | `INPUT_ROW_LIMIT_EXCEEDED` | 超过 `max_source_rows` | 400 |
| `STATE_CONFLICT` | `STATE_TARGET_DUPLICATE_KEY` | 目标快照存在重复键 | 409 |
| | `STATE_TARGET_TABLE_MISSING` | 目标表不存在/不可读 | 409 |
| | `STATE_SNAPSHOT_STALE` | 计划后、执行前目标被并发修改 | 409 |
| | `STATE_CONSTRAINT_VIOLATION` | 执行期约束/触发器冲突（已回滚） | 409 |
| `RESOURCE_EXHAUSTED` | `RESOURCE_DB_LOCKED` | SQLite 加锁/忙碌 | 503 |
| | `RESOURCE_COMMIT_FAILED` | 提交失败（含注入故障），事务回滚 | 503 |
| `COMPUTATION_FAILURE` | `COMPUTE_PREDICATE_EVALUATION` | 条件/赋值运行期错误（除零、类型不可比等） | 422 |

> 行上限被归入 `INPUT_ERROR`：它是请求级资源契约；真正的资源耗尽（锁、提交失败）才是
> `RESOURCE_EXHAUSTED`。`TC-ERR-03/04/05/06` 区分「请求写错（400）」与「数据算不动（422）」。

---

## 4. 模块关系与数据/错误契约

```
HTTP (api.py, FastAPI)
  │  JSON {merge: spec, source: {...}, options?:{failpoint_commit}}
  ▼
service.py MergeService  ── 编排/事务边界；分配 run_id；写元数据与 JSONL
  ├─ adapter.py    records / pyarrow.Table / Arrow-IPC bytes → list[SourceRow] + 指纹
  │                错误：INPUT_ERROR(SCHEMA/UNSUPPORTED_VALUE/ROW_LIMIT)
  ├─ snapshot.py   目标表 schema + 操作前快照(rowid+所需列) + 目标重复键扫描 + 指纹
  │                错误：STATE_CONFLICT(TARGET_DUPLICATE/TABLE_MISSING)，INPUT(列缺失)
  ├─ predicate.py  SQL 方言→AST；编译期列/函数白名单（INPUT_ERROR），运行期 3VL 求值错误
  ├─ planner.py    纯决策内核：prepare(编译/列契约) + build_plan(目标重复→源重复→逐行求值)
  │                输出 MergePlan(actions, decisions, 指纹, 快照)；无 SQL、无连接
  ├─ executor.py   单 BEGIN IMMEDIATE：陈旧复核→UPDATE/INSERT/DELETE→一次 commit/失败 rollback
  │                错误：STATE_CONFLICT / RESOURCE_EXHAUSTED / COMPUTATION_FAILURE
  ├─ metadata.py   merge_runs / merge_actions / merge_traces / merge_snapshots（独立于数据事务）
  └─ runlog.py     logs/merge-runs.jsonl：RUN_START/SNAPSHOT/PLAN/VALIDATE_ONLY/
                   RUN_COMMIT/RUN_REJECT/RUN_FAIL/COMMIT_FAULT
```

- **数据契约**：跨层只传 `contract.py` 中的冻结 dataclass（`MergeSpec / WhenClause /
  SourceRow / TargetRow / PlannedAction / Decision / MergePlan`）。规划器输出的
  `MergePlan` 就是「验证接口 ↔ 执行内核」之间唯一契约：`/validate` 返回它，`executor`
  只消费它。
- **错误契约**：所有异常都是 `errors.MergeError`，携带稳定 `category` + `code` +
  `details`；各模块允许抛出的类别见上图与第 3 节。API 统一错误信封
  `{"error":{category,code,message,details},"run_id":...}`。
- **原子性边界**：元数据写在数据事务之外（计划落库后执行；执行失败数据回滚但审计完整）。

### 可重放日志

每条 JSONL 行都带 **run_id + 单调 run_seq + UTC 时间戳 + 事件类型**，并保留关键中间状态：
源行与源指纹（RUN_START）、操作前目标快照/目标指纹/重复键组（SNAPSHOT）、动作集合与逐行
判定理由（PLAN）、终态（RUN_COMMIT/RUN_REJECT/RUN_FAIL）及提交故障（COMMIT_FAULT）。仅凭
run_id 即可从 SQLite 元数据（spec + 源 + 目标快照 + 动作）或 JSONL 重放问题；
`scripts/replay.py verify` 用独立 oracle 比对动作集合。

### 测试参考答案的独立性

`tests/oracle.py` **不 import 任何 `merge_engine.*`**：它用标准库独立实现了一份递归下降
表达式求值器（自带 3VL）与匹配/优先级决策，返回期望动作集合与重复键组。`TC-PLAN-01` 将
被测规划器结果逐字段对齐该 oracle；`scripts/replay.py verify` 对已落库运行做同样复核。

---

## 5. 目录

```
merge_engine/   errors contract predicate utils adapter snapshot planner
                metadata executor runlog service api
tests/          conftest.py(合成夹具) oracle.py(独立参考答案)
                test_planner.py test_atomicity.py test_invariants.py test_api_log.py
scripts/        seed_demo.py run_demo.py replay.py
examples/       demo_merge.json
requirements.txt  pytest.ini
```

---

## 6. 依赖版本（已锁定并验证）

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.12.3（≥3.10 可用） | 运行时 |
| pyarrow | 18.1.0 | Arrow Table / IPC 源适配 |
| fastapi | 0.115.6 | HTTP 边界 |
| pydantic | 2.10.4 | FastAPI 依赖（项目主体用 dataclasses） |
| uvicorn | 0.34.0 | 本地 ASGI 服务 |
| pytest | 8.3.4 | 测试框架 |
| httpx | 0.28.1 | TestClient 传输 |
| SQLite | 3.42.0（系统内置） | 目标数据 + 元数据事务 |

查看实际版本：`.venv/bin/python -c "import pyarrow,fastapi,pydantic,uvicorn,pytest,httpx,sqlite3; ..."`。

---

## 7. 测试结果与状态（如实标记）

- **已运行并通过：45/45**（本机、CPython 3.12.3、上述锁定版本）。
- **未运行/未通过**：无未通过用例；`TC-ATOM-06`（数据库锁 → RESOURCE_EXHAUSTED）依赖本机
  线程时序，已用极短 busy_timeout 稳定复现，在持续高负载 CI 上仍属时序敏感用例，若偶发可
  单独重跑 `pytest -k tc_atom_06`。除此之外全部为确定性用例。
- 空批次（`TC-EDGE-01`）定义为合法 no-op：空源无 schema 可绑定表达式，规划器走快速路径
  出空计划（仍校验目标表存在）；HTTP 冒烟（真实 uvicorn + curl）、`--fail-commit`
  回滚、`replay verify` 均已手动执行确认。

### 用例 → 需求覆盖

| 需求 | 用例 |
|---|---|
| 源内同键多行拒绝、不依赖行序 | TC-PLAN-04/05, TC-INV-01 |
| 匹配只用操作前快照 | TC-PLAN-03/10, TC-INV-02/03/04/05 |
| NULL 策略与规则优先级 | TC-PLAN-05/06/07/08 |
| 目标重复键 | TC-PLAN-09 |
| 更新影响条件（条件依赖 T.*/S.*）手工核验 | TC-PLAN-01/02, TC-PLAN-07 |
| 源重复冲突 | TC-PLAN-04/05, TC-API-03, TC-LOG-04 |
| 手工核验动作集合 | TC-PLAN-01/02, demo + replay verify |
| 注入提交失败、无部分更新 | TC-ATOM-01, TC-API-03, TC-LOG-03 |
| 断言具体结果与失败类别 | 全部 TC-* 均断言值/键/计数 + category/code |
| 参考答案不由被测核心生成 | tests/oracle.py 零引擎依赖 |
| 四类错误可区分 | TC-ERR-01..07, TC-ATOM-04/05/06, TC-API-03 |
| 可重放日志（运行编号/中间状态/理由） | TC-LOG-01..05, scripts/replay.py |
