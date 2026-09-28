# RTDA — 湖表读时删除应用器（Read-Time Delete Applier）

纯后端服务：模拟湖仓（Iceberg 风格）的数据文件 / 删除文件分离模型，在**读取时**应用
两类删除——**文件行号删除（position delete）**与**主键等值删除（equality delete）**。
技术栈：Python 3.12 · FastAPI · PyArrow（Parquet）· SQLite（元数据事务）。
所有数据均为本地合成夹具，无外部账号与真实业务数据依赖。

---

## 1. 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt        # 固定版本；完整传递依赖见 requirements.lock

# 跑全部测试（49 个）
pytest -q

# 端到端验证（三方对照 + 证据报告）
python scripts/verify.py
# -> reports/verify-report-<编号>.json，内含 run_id、每版本逐行保留/删除依据

# 启动 HTTP 服务
./scripts/run_server.sh                # 或：uvicorn app.api.app:app_factory --factory --reload
curl -s localhost:8000/healthz
```

仓库默认在 `.warehouse/`（Parquet + `metadata.db`），可用 `RTDA_WAREHOUSE` 覆盖。

---

## 2. 工程边界（模块契约）

| 层 | 模块 | 职责 | 不允许做的事 |
|---|---|---|---|
| 契约 | `app/contracts/` | 逻辑类型、NULL 比较规则、接口请求模型 | 不 import pyarrow |
| 格式适配 | `app/adapters/pyarrow_ops.py` | Parquet 读写、严格类型映射、内容 SHA-256、行/元数据完整性 | 不做删除判定 |
| 执行内核 | `app/kernel/scanner.py` | 读时应用：序列号窗口、位置/等值命中、先删后过滤、列裁剪 | 不写元数据 |
| 执行内核 | `app/kernel/filters.py` | 过滤 DSL（三值逻辑） | 不改变删除结果 |
| 独立参考 | `app/kernel/oracle.py` | **纯标准库**第二实现，逐版本给期望；不读 Parquet/SQLite | 不与 scanner 共享代码 |
| 元数据事务 | `app/metadata/store.py` | 表/快照/清单/删除文件/事件/运行日志；单事务提交 | 不碰业务判定 |
| 服务 | `app/services/planner.py` | 操作解析、规范化与全部语义校验（validate 与 commit 共用） | 不落地 |
| 服务 | `app/services/committer.py` | 先写物理文件、单事务发布快照、失败回滚清理 | — |
| 验证接口 | `app/api/app.py` | FastAPI 路由、统一错误信封、run_id | 不含判定逻辑 |

错误契约统一为：

```json
{"error": {"category": "...", "code": "...", "message": "...", "details": {}},
 "run_id": "run-...."}
```

| category | HTTP | 含义 | 典型 code |
|---|---|---|---|
| `VALIDATION_ERROR` | 400 | 输入错误 | `TYPE_MISMATCH`、`POSITION_OUT_OF_RANGE`、`DUPLICATE_POSITION`、`MISSING_KEY_VALUE`、`INVALID_FILTER` |
| `STATE_CONFLICT` | 409 | 状态冲突 | `PARENT_MISMATCH`、`STALE_POSITION_TARGET`、`FILE_NOT_LIVE`、`POSITION_TARGET_UNKNOWN` |
| `RESOURCE_EXHAUSTED` | 413 | 资源耗尽 | `TOO_MANY_ROWS`、`TOO_MANY_FILES`、`TOO_MANY_DELETE_FILES` |
| `COMPUTATION_FAILED` | 500 | 计算/完整性失败 | `CONTENT_HASH_MISMATCH`、`ROW_COUNT_MISMATCH`、`PARQUET_READ_FAILED`、`INTERNAL_ERROR` |
| `NOT_FOUND` | 404 | 资源不存在 | `TABLE_NOT_FOUND`、`SNAPSHOT_NOT_FOUND`、`RUN_NOT_FOUND` |

输入错误（形状/类型/范围）与状态冲突（引用的文件状态已变化）刻意分开；
资源上限在建表 `config` 中可配（`max_rows_per_data_file` 等）。

---

## 3. 边界语义（删除正确性的定义）

### 3.1 版本、序列号与文件身份

- 每次成功提交生成一个**快照**，分配表内严格递增的 `seq`（从 1 起）。
- 提交必须携带 `parent_snapshot_id`（乐观并发；父快照过期 → `PARENT_MISMATCH/409`）。
  **失败提交不消耗序列号**，其后的成功提交仍取得原本的下一个 seq。
- 数据文件**不可变**，身份 = `file_id + content_hash(SHA-256)`。
- **重写（rewrite）= 新文件 ADD + 旧文件 DROP**（清单仅追加，带 `reason=REWRITE`）。

### 3.2 位置删除：绑定原文件内容身份，旧行号不复用

- 位置删除记录 `(target_file_id, position)`，position 为 0 基行号，只对目标文件当前内容生效。
- 文件重写后旧文件 DROP、新文件是全新身份；对旧文件 id 再发行号删除 →
  **`STALE_POSITION_TARGET / 409`**，新文件从行号 0 重新计数，绝不继承旧行号。
- 行号越界 → `POSITION_OUT_OF_RANGE / 400`（输入错误，区别于上面的状态冲突）。
- 同一次操作内重复行号 → `DUPLICATE_POSITION / 400`。
- 位置删除不能指向**同一提交**新增的文件（目标文件 seq 必须严格更早）→
  `POSITION_TARGET_SAME_COMMIT / 400`。
- 位置删除可指向任意其他存活文件（**跨文件删除**）。

### 3.3 等值删除：按序列号可见范围应用

- 等值删除文件带序列号 `dseq` 与键值元组；它删除满足
  **`data_file.added_seq < dseq`（严格小于）** 的存活数据文件中键相等的行。
- 因此：**先删后插**——同快照或后来插入的同键行不会被旧删除波及；需要再次提交删除才命中。
- **重复键**：同一/不同旧文件中的所有匹配旧行都被删除（一对多）。
- 跨文件：一个等值删除对窗口内所有存活文件生效。

### 3.4 NULL 键比较（SQL 语义）

- 删除向量中任一键列为 NULL 的谓词**不匹配任何行**（即使数据行键也是 NULL）；
  接口要求显式给出 NULL（缺列是 `MISSING_KEY_VALUE / 400`，与“显式 NULL 不命中”区分）。
- 数据行键为 NULL 时，永不被等值删除命中。
- 被忽略的 NULL 键谓词数量在 explain 的 `null_keys_ignored` 中可观测。

### 3.5 过滤与列裁剪不得改变删除语义

- 内核固定顺序：**解析版本 → 位置删除 → 等值删除 → 过滤 → 投影**。
- 被删行不参与过滤：即使它满足过滤条件，explain 中仍标 `DELETED`，不会“复活”；
  `/rows` 中也永不出现。
- 投影可以省略键列；扫描器内部读取集合 = 投影列 ∪ 过滤引用列 ∪ 全部等值键列，
  删除判定所需列始终读取（证据见 explain 的 `intermediate_state.read_columns`）。

### 3.6 时间旅行与读一致性

- `/rows`、`/explain` 支持 `snapshot_id` 或 `seq` 指定版本；
  存活文件 = 该版本前清单的最新事件为 ADD；删除文件仅计入 `dseq <= 版本seq`。

### 3.7 类型与值

- 逻辑类型：`string / long(int64) / int(int32) / double / boolean / date(YYYY-MM-DD)`。
- 严格转换：布尔不接受 0/1；整数超界、NaN/Infinity、非法日期均为 `TYPE_MISMATCH / 400`。

---

## 4. HTTP 接口摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/tables` | 建表（columns、primary_key、资源 config） |
| POST | `/tables/{id}/commits` | 提交：`append` / `rewrite` / `position_delete` / `equality_delete` 可混合 |
| POST | `/tables/{id}/validate` | 只走规划校验，不落地；错误码与 commit 完全一致 |
| GET | `/tables/{id}/rows?snapshot_id=&seq=&columns=a,b&filter=<JSON>` | 存活行（删除+过滤后） |
| POST | `/tables/{id}/explain` | 全量逐行 `KEPT/DELETED/FILTERED` + `reasons`（规则、删除文件、序列号、命中键） |
| GET | `/tables/{id}/snapshots` | 逐版本 |
| GET | `/tables/{id}/events?seq=` | 逐版本领域事件（可按版本裁剪） |
| GET | `/tables/{id}/files` | 当前存活文件与 ADD/DROP 清单（含内容哈希） |
| GET | `/runs/{run_id}` | 运行日志：分阶段中间状态、判定理由、错误分类、耗时 |

提交体示例：

```json
{
  "table_id": "tbl-...",
  "parent_snapshot_id": "snap-...",
  "operations": [
    {"op": "rewrite", "ref": "f-new", "drops": ["file-old"],
     "rows": [{"id": 10, "name": "ten"}]},
    {"op": "position_delete", "target_file": "file-other", "positions": [0, 3]},
    {"op": "equality_delete", "predicates": [{"key": {"id": 5}}, {"key": {"id": null}}]}
  ]
}
```

响应回填 `files[].file_id`（ref → 服务端文件 id 映射）与 `delete_files[]`。

---

## 5. 可复核输出（三方对照，参考答案不由被测实现生成）

每个夹具场景（`fixtures/scenarios/*.json`）同时由三方回答并逐行比对：

1. **手写期望**：JSON 中每版本 `expect.dispositions`（含删除理由 `(kind, seq)`）与错误版本；
2. **独立 oracle**：`app/kernel/oracle.py`，纯标准库、独立重放规则；
3. **被测系统**：真实 HTTP 调用 FastAPI，经 PyArrow 读 Parquet、SQLite 查元数据。

比对项：行身份集合 `(file_id, position)`、逐行处置、删除理由多重集、存活文件集合、
时间旅行读取值、错误提交的 category/code。任何不一致以具名失败类别中断：
`RESULT_SET_MISMATCH / DISPOSITION_MISMATCH / REASON_MISMATCH / FILE_SET_MISMATCH /
ERROR_CATEGORY / ORACLE_VS_HANDWRITTEN / VALUE_MISMATCH / API`。

测试覆盖（`tests/`，49 个）：

- **重写文件**后旧行号拒绝（`STALE_POSITION_TARGET`），新文件行号重新计数；
- **先删后插**：同键后插入存活、二次删除才命中，并断言理由序列号；
- **重复键 / NULL 键**（删除向量与数据双侧 NULL）/ 复合主键；
- **跨文件**位置与等值删除、同一行被两类删除同时命中（理由并存）；
- 过滤/投影不改变删除语义、时间旅行、严格类型；
- 四类失败可区分、错误信封形状、validate 与 commit 同错；
- 篡改 Parquet → `CONTENT_HASH_MISMATCH`；失败提交回滚且不消耗序列号；
- 逐版本事件顺序、运行日志中间状态与可重放 run_id。

`python scripts/verify.py` 产出 `reports/verify-report-<UTC编号>.json`：
每个版本、每个文件、每一行的 `disposition` 与 `basis`（哪条删除文件、seq、命中键），
以及该错误版本的 run_id（可用 `GET /runs/{run_id}` 重放完整判定过程）。

---

## 6. 明确不做的事 / 未执行的检查

以下项目**未实现或未验证**，不得视为已通过：

- **未做** schema 演进（列增删/类型变更）；表 schema 建表后固定。
- **未做** 多节点并发压测与锁竞争实测；并发安全仅由“父快照乐观校验 + BEGIN IMMEDIATE
  + busy_timeout”保证，有 `PARENT_MISMATCH` 用例但没有高并发基准数据。
- **未做** 删除文件合并（compaction）、真空物理删除旧文件；旧文件永久保留在磁盘。
- **未做** 真实对象存储 / 外部 catalog / 鉴权 / 多租户隔离；全部本地文件系统。
- **未做** 分区、列统计、向量化下推；扫描为全文件读取（合成数据规模下足够）。
- **未做** 浮点 `-0.0`、超大字符串、字符排序规则（collation）等边界专项。
- 依赖安装与测试仅在本机 Linux + Python 3.12 环境执行；其他平台未验证。
