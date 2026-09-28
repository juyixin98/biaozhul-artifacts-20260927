# 不可变表快照三方合并后端

开发分支（dev）与主分支（main）各自持有**不可变表快照**的提交历史；后端以共同祖先
（merge base）做**行级三方合并**，自动合入无冲突分区，显式保留冲突分类并要求解决动作
**绑定三方快照**，合并提交保留**两条父引用**。

- 技术栈：Python 3.10+ · FastAPI · PyArrow（Parquet 快照）· SQLite（元数据/血缘）
- 全部数据为本地合成夹具，无需任何生产账号

---

## 1. 快速开始

```bash
bash run.sh setup        # 创建 .venv 并安装依赖（仅首次）
bash run.sh test         # 运行全部单元 + 集成测试（日志同时落 logs/）
bash run.sh demo         # 进程内端到端演示（临时存储，跑完即删）
bash run.sh serve        # 启动 HTTP 服务  http://127.0.0.1:8000/docs
```

不用脚本的等价命令：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
.venv/bin/python scripts/demo.py
.venv/bin/python -m uvicorn table_merge.api:app --app-dir src --port 8000
```

最近一次真实运行结论（完整输出见 [`docs/test_output.txt`](docs/test_output.txt)、
[`docs/demo_output.txt`](docs/demo_output.txt)）：

```
83 passed, 1 warning in 1.09s
DEMO OK: 自动合并分区、三类冲突、血缘双亲引用均已验证。
```

HTTP 模式演示（先 `bash run.sh serve`，再开第二个终端）：

```bash
bash run.sh demo-http     # 等价于 .venv/bin/python scripts/demo.py --http
```

---

## 2. 合并语义（验收行为约定）

行身份是**主键值**（不是文件名）；字段逐列比较。对每个主键 K，取
base/dev/main 三方行 B/D/M：

| 情形 | 判定 | 自动结果 |
|---|---|---|
| D==B 且 M==B | `UNCHANGED` | 取 B |
| 仅一侧变化（含仅一侧删除） | `FAST_FORWARD` | 取变化侧；删除则行消失 |
| 两侧都改、改动字段集合**不相交** | `FIELD_MERGE` | 逐字段取变化侧 |
| 同字段两侧改成**相同值** | `FIELD_MERGE`（收敛） | 取该值，不算冲突 |
| 两侧改**同一字段且值不同** | `SAME_FIELD_CONFLICT` | 挂起，待解决 |
| 一侧删除、另一侧**修改** | `DELETE_MODIFY_CONFLICT` | 挂起，待解决 |
| 仅一侧新增 | `FAST_FORWARD` | 取新增行 |
| 两侧新增同主键、内容不同 | `ADD_ADD_CONFLICT` | 挂起，待解决 |
| 两侧新增同主键、内容相同 | `FIELD_MERGE`（收敛） | 取任一侧 |

冲突解决动作（每类冲突允许的动作集合由内核强校验）：

- `USE_DEV` / `USE_MAIN`：采用对应一侧的行；
- `KEEP_DELETED`：接受删除（仅删改冲突）；
- `FIELD_PICK`：逐字段选 `DEV`/`MAIN`（同字段冲突可用，新增/新增与删改不可用）。

**禁止路径：**

- 未解决全部冲突时提交合并 → `409 CONFLICT_STATE`，不会返回成功；
- 解决记录绑定 `(base, dev, main)` 三方快照 ID；计划输入变化会得到新 `plan_id`，
  拿旧计划解决/提交 → `409` 并明确报 `stale`；
- 重新导入相同内容命中内容去重（同一 snapshot_id），**无法**用重新导入主分支快照
  覆盖或回退分支历史；分支推进带乐观并发检查（旧头提交会被拒绝）。

---

## 3. HTTP 工作流

所有响应头回显 `X-Request-ID`；不传则服务端生成 `run_xxx`，服务端日志全程带同一 ID。

```bash
# 1) 摄取 base/dev/main 三个快照（JSON 行集 -> Parquet）
curl -s -X POST localhost:8000/api/v1/snapshots \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: run-demo-1' \
  -d '{"schema":{"table":"employees",
        "columns":[{"name":"id","type":"int64"},{"name":"city","type":"string"}],
        "primary_key":["id"]},
       "rows":[{"id":1,"city":"bj"}]}'

# 2) 初始化 main，从 base 提交建 dev 分支，各自提交（见 scripts/demo.py 的 HTTP 版）
# 3) 生成合并计划（无冲突分区已自动合并，冲突逐行给出 decision + basis）
curl -s -X POST localhost:8000/api/v1/merges/plan \
  -H 'Content-Type: application/json' \
  -d '{"dev":{"branch":"dev"}}'

# 4) 解决冲突（row_key 是主键 JSON，如 [4]）
curl -s -X POST localhost:8000/api/v1/merges/resolve \
  -H 'Content-Type: application/json' \
  -d '{"plan_id":"plan_xxx","dev":{"branch":"dev"},
       "resolutions":[{"row_key":"[4]","action":"USE_MAIN"}]}'

# 5) 提交合并（响应含两条 parent_commit_ids 与 resolution_summary）
curl -s -X POST localhost:8000/api/v1/merges/commit \
  -H 'Content-Type: application/json' \
  -d '{"plan_id":"plan_xxx","dev":{"branch":"dev"},"message":"merge"}'

# 6) 血缘查询（is_merge=true 时含 base 与双亲、三方快照、解决动作统计）
curl -s "localhost:8000/api/v1/lineage?commit_id=merge_xxx"
```

错误一律返回结构化 `{"error_code", "message", "details"}`，分类包括
`NOT_FOUND(404)`、`CONFLICT_STATE(409)`、`SCHEMA_MISMATCH(422)`、
`INVALID_PAYLOAD(422)`、`INVALID_RESOLUTION(422)`、`SNAPSHOT_FORMAT(422)`、
`NO_COMMON_ANCESTOR(422)`、`INTERNAL_ERROR(500)`。异常/未知状态不会被统一成成功。

---

## 4. 工程结构（按四层组织）

```
src/table_merge/
├── config.py            # 配置层：YAML + TABLE_MERGE_* 环境变量覆盖
├── format_adapter.py    # 格式适配：JSON 行集 <-> PyArrow <-> Parquet，严格类型校验、确定性写出
├── merge_kernel.py      # 执行内核：纯函数三方合并 + 冲突分类 + 解决合法性/应用（无 I/O）
├── storage.py           # 元数据事务：SQLite（BEGIN IMMEDIATE 单事务）+ 内容寻址快照
├── service.py           # 用例编排：摄取/分支/提交/共同祖先/计划/解决/合并提交
├── api.py               # 验证接口：FastAPI、run_id 中间件、错误码映射
├── models.py            # 领域模型：schema/快照/行级判定/合并报告/枚举
├── errors.py            # 业务异常与错误码
└── logging_setup.py     # run_id + 版本号的结构化日志
scripts/demo.py          # 进程内 / HTTP 两种端到端演示
sample_data/employees.json  # 覆盖四类验收场景的合成夹具（含 expected_decisions）
config/dev.yaml          # 本地配置
tests/
├── oracle.py                       # 独立对照预言机（另写的一份合并实现，不 import 被测内核）
├── test_merge_kernel.py            # 六类判定 + 解决动作分类 + 字面量结果断言
├── test_fuzz_against_oracle.py     # 40 组随机三方行集，逐键交叉验证内核 vs 独立预言机
├── test_storage.py                 # 幂等去重、事务原子性、共同祖先、禁止覆盖历史
├── test_format_adapter.py          # 类型/主键/缺列、Parquet 往返与字节确定性
├── test_api_integration.py         # 全流程、状态码/错误类别、过期计划、run_id
└── test_observability.py           # 日志含 run_id/版本/步骤/判定依据
```

设计要点：

- **不可变 & 内容寻址**：Parquet 写前按主键排序、固定压缩/行组，同内容同字节、
  同 sha256、同 snapshot_id；重复导入只复用，不产生新历史。
- **事务边界**：文件写盘独立且幂等；`snapshots/commits/merge_commits/branches`
  的元数据变更在一个 `BEGIN IMMEDIATE` 事务内，失败整体回滚。
- **共同祖先**：沿提交的第一父边（`commits.parent_commit_id`）与合并提交的
  dev 父边（`merge_commits.parent1_dev_id`）双向 BFS 求最近共同祖先。
- **双亲血缘**：合并提交在 `merge_commits` 中保存 `base / parent1_dev /
  parent2_main` 三个提交与三个快照；`/lineage` 返回 `parent_commit_ids` 两条父引用。
- **计划不可变**：`plan_id = hash(base_snap, dev_snap, main_snap, target_branch,
  head)`，计划不占服务端会话状态，可随时按相同输入重建；解决记录按 plan_id 落库，
  提交时逐条复核其绑定的三方快照。

---

## 5. 测试与可核验性

- 测试断言**具体结果与失败类别**（具体主键、字段值、行集集合、HTTP 状态码与
  `error_code`），不是仅检查“接口能调用”；
- 参考答案来自三处互相独立的来源：测试内手写字面量、`tests/oracle.py`
  的独立实现、40 组随机夹具（fuzz）交叉验证——参考答案不由被测内核自身生成；
- 测试日志 `logs/tests.log` 每行带 `run_id=tests_…`，记录 `test_start`、
  `kernel_step step 1/4..4/4`、每个冲突的 `decision` 与 `basis`；
- `sample_data/employees.json` 内置 `expected_decisions`，标注每个主键的预期判定。
