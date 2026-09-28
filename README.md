# 简化湖表元数据事务服务

基于**快照版本**的本地湖表元数据事务原型：数据是本地、不可变的 Parquet 文件，
元数据用 SQLite 管理，服务用 FastAPI 暴露验证接口。它实现了类似 Apache Iceberg
乐观并发的一个**最小子集**，用于验证以下规则，而不是替代 Iceberg。

> **明确说明：这不是完整 Iceberg 实现。** 不支持多 catalog、模式演进、分区规范演进、
> 行级删除/equality delete、清单文件层级（manifest list）、对象存储、分布式锁、
> 时间旅行之外的历史保留策略等。快照链、内容寻址文件、乐观提交是受 Iceberg 启发的简化模型。

---

## 1. 它解决什么问题

1. **提交基于快照版本**：每个提交声明 `base_snapshot_id`；成功后产生新的不可变快照，
   旧快照与旧数据文件永不原地改写。
2. **互不相交分区的追加可按声明规则重试合并**：基线陈旧但与并发提交不相交的 `APPEND`，
   服务以当前表头为父快照自动合并接受（响应中 `merged=true`）。
3. **重叠分区覆盖必须检测冲突，不能最后写获胜**：`OVERWRITE` 遇到并发提交时
   明确拒绝并归类（`CONCURRENT_OVERWRITE` / `PARTITION_CONFLICT` / `STALE_BASE_OVERWRITE`）。
4. **文件先完整写入再挂入快照**：先写暂存区临时文件并读回校验（Parquet 可解析、
   模式一致、sha256 一致、行数一致），通过后才发布到数据区并写入快照清单。
5. **失败文件有独立清理记录**：每个失败/已发布/孤立文件在 `cleanup_ledger` 中各有一条记录，
   文件移入**隔离区**而非直接删除。

## 2. 模块职责（真实分层，非单文件脚本）

| 模块 | 职责 | 是否含 I/O |
| --- | --- | --- |
| `lake_txn/config.py` | TOML 配置、仓库路径约定、import 白名单 | 读配置 |
| `lake_txn/errors.py` | 稳定的 `reason_code` 失败类别与异常 | 否 |
| `lake_txn/diagnostics.py` | 关联标识（HTTP/请求 id）、结构化 JSON 日志、递归脱敏 | stderr |
| `lake_txn/format_adapter.py` | 记录↔PyArrow↔Parquet、原子写、sha256、读回校验 | 文件 |
| `lake_txn/kernel.py` | **纯逻辑内核**：冲突裁决 `adjudicate`、清单规划 `plan_manifest` | **否** |
| `lake_txn/metadata.py` | SQLite：表/快照/清单/提交日志/暂存台账/清理台账，单锁串行 | SQLite |
| `lake_txn/service.py` | 编排：建表、暂存发布、两阶段提交、孤立文件清扫 | 编排全部 |
| `lake_txn/api/__init__.py` | FastAPI 请求模型、错误映射、`X-Request-ID`、端点 | HTTP |
| `lake_txn/main.py` | 本地启动入口 | — |
| `tests/` | 独立测试（内核穷举、格式、服务、真实并发、HTTP、清理） | tmp 目录 |
| `fixtures/` | 最小合成夹具生成器 | 写夹具 |
| `examples/` | Python 直调、HTTP 客户端、curl 调用示例 | — |
| `config/service.toml` | 独立配置 | — |
| `scripts/reproduce.sh` | 一键复现 | — |

## 3. 裁决规则（`lake_txn/kernel.py`）

设提交基线为 B，当前表头为 H，B 之后已成功的提交为“并发提交”：

- `APPEND`，B == H：直接接受。
- `APPEND`，B < H，与所有并发提交触及分区**不相交**：自动合并（父快照取 H）。
- `APPEND` 与并发 `APPEND` 分区相交：`PARTITION_CONFLICT`，响应给出**具体重叠分区**。
- `APPEND` 与并发 `OVERWRITE` 分区相交：`CONCURRENT_OVERWRITE`；不相交则合并。
- `OVERWRITE`，B == H：接受（替换 `drop_partitions`，新文件必须落在该范围）。
- `OVERWRITE`，B < H：
  - 有并发 `OVERWRITE`：`CONCURRENT_OVERWRITE`（即使分区不相交，也不自动重放覆盖）；
  - 仅有并发 `APPEND` 且范围相交：`PARTITION_CONFLICT`；
  - 仅有并发 `APPEND` 且范围不相交：`STALE_BASE_OVERWRITE`（请刷新后重放）。
- B > H：`BASE_IN_FUTURE`；B 不存在：`UNKNOWN_BASE`。
- 相同 `request_id` 的重放：返回首次结果（幂等）；同一 `request_id` 改了请求体：
  `REQUEST_SCOPE_MISMATCH`。

冲突裁决是无 I/O 纯函数，可独立穷举测试（见 `tests/test_kernel.py`）。

## 4. 数据与文件生命周期

```
仓库根 (config: warehouse.root)
├── metadata.sqlite3        # 表/快照/清单/提交日志/暂存台账/清理台账
├── staging/<request_id>/   # 暂存区：原子写 + 读回校验，提交成功后删除
├── tables/<table>/data/<partition>/<sha256>.parquet   # 不可变数据文件
└── quarantine/
    ├── stage_failed/       # 暂存/发布失败文件
    ├── orphan_staging/     # 清扫出的孤立暂存文件
    └── orphan_data/        # 不在任何快照清单中的孤立数据文件
```

- 数据文件按**内容 sha256 命名**，天然不可变、去重；覆盖分区时新增新文件，
  旧文件只从新清单消失（仍被旧快照引用）。
- 提交事务顺序：物理校验暂存文件 → `BEGIN IMMEDIATE` 串行事务 → 内核裁决 →
  拒绝则只记 `commit_log`；接受则**先发布物理文件，再插入快照+清单+提交日志**，最后提交事务。
- 发布中途失败：不产生快照；已发布文件逐个隔离，`commit_log` 记 `PUBLISH_FAILED`，
  暂存条目标记 `failed`，客户端需重新暂存后用**新 request_id** 提交。
- 清扫器只处理“超过宽限期且无任何台账/清单引用”的文件；在途就绪文件绝不删除。

## 5. 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock          # 完整传递锁定

python -m pytest                           # 运行全部测试
python examples/usage_demo.py              # 进程内调用演示（含并发/冲突/重放/清扫）

uvicorn lake_txn.api:create_app --factory --port 8000   # 启动 HTTP 服务
python examples/http_client_demo.py        # 另一个终端：HTTP 正常+冲突演示
bash examples/curl_example.sh              # 或用 curl
```

一键复现（建环境、锁定安装、夹具、测试、进程内演示、真实 HTTP 演示，结果落 `results/`）：

```bash
bash scripts/reproduce.sh
```

## 6. HTTP 接口摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/tables` | 建表（列模式 + 分区列） |
| GET | `/v1/tables` | 列表及各表表头快照 |
| POST | `/v1/staging/files` | 暂存文件（`inline` 服务写 Parquet / `import` 白名单本地文件） |
| POST | `/v1/commits` | 提交（`APPEND`/`OVERWRITE`） |
| GET | `/v1/commits/{request_id}` | 按幂等键查提交结果（响应丢失后判定，未知请求返回 `UNKNOWN_REQUEST` 400） |
| GET | `/v1/tables/{t}/snapshots` | 快照链 |
| GET | `/v1/tables/{t}/snapshots/{id}` | 快照清单详情 |
| POST | `/v1/maintenance/sweep` | 孤立文件清扫（可传 `grace_seconds`） |
| GET | `/v1/maintenance/cleanup-ledger` | 逐文件清理记录 |

所有响应带 `X-Request-ID` 关联标识；错误统一为
`{error, reason_code, message, detail}`，其中 `detail` 给出基线、表头、并发请求、
重叠分区等关键状态，说明**为什么接受、拒绝或无法判定**。

### 暂存两种模式

- `inline`：提交 JSON 记录，服务转 Parquet（严格模式校验、原子写、读回校验）。
- `import`：引用**白名单目录**（配置 `security.allowed_inbound_dirs`）中的本地 Parquet，
  硬链接（跨设备回退拷贝）进暂存区；目录外一律 `IMPORT_PATH_FORBIDDEN`。

## 7. 测试如何验证（断言具体结果，而非“接口能调用”）

- `tests/test_kernel.py`：对纯内核穷举每个接受/拒绝类别与具体重叠分区、清单结果。
- `tests/test_service_basic.py`：用**独立物理 oracle**（独立 sqlite3 连接 + PyArrow
  直接扫描清单文件）断言每个快照的**具体行集**，并验证旧快照不可变、覆盖只改目标分区。
- `tests/test_concurrency.py`：真实线程 + 屏障并发，验证：
  - 互不相交分区追加全部接受（除首个外全部 `merged=true`，行集不丢不重）；
  - 同分区追加恰有一个赢、其余 `PARTITION_CONFLICT`，刷新重试后全部追加成功；
  - 同分区覆盖恰有一个赢，输家为 `CONCURRENT_OVERWRITE`/`PARTITION_CONFLICT`，
    未覆盖分区原样保留；
  - 陈旧覆盖与并发追加不相交时是 `STALE_BASE_OVERWRITE`（明确拒绝而非静默覆盖）。
- `tests/test_idempotency_cleanup.py`：响应丢失重放返回同一快照且行不翻倍；
  request_id 复用不同范围被拒；未知 request_id 显式“无法判定”；失败文件逐个隔离+台账；
  孤立暂存目录/孤立数据文件清扫（在途与宽限期文件不动）；发布失败无快照、逐文件记录；
  敏感字段（ssn/email/token）日志中脱敏。
- `tests/test_api.py`：HTTP 状态码、`reason_code`、关联头、import 白名单。

**参考答案独立性**：测试 oracle 用独立连接和 PyArrow 自行计算期望行集，
不调用被测服务的读路径；冲突期望值由测试用例根据规则直接给出。

## 8. 诊断与脱敏

- 每条日志是一行 JSON，含 `event` 与 `http_request_id`，提交相关事件还含业务 `request_id`。
- 接受/拒绝事件记录基线、表头、并发请求 id、重叠分区、裁决码等关键状态。
- 配置 `security.redact_fields` 中的字段（默认 ssn/email/token/secret/password/id_card）
  在写日志前对 dict/list 递归脱敏，值替换为 `***REDACTED***`。可运行
  `pytest tests/test_idempotency_cleanup.py::test_diagnostics_redacts_sensitive_fields -q`
  复核原始敏感值不出现在日志中。

## 9. 本地数据与配置

- 无生产账号、无外部服务；所有夹具均为虚构合成数据。
- 默认仓库在 `./.local-data/warehouse`（被 `.gitignore` 忽略）。
- 配置文件 `config/service.toml`，可用环境变量 `LAKE_TXN_CONFIG` 覆盖。

## 10. 已知简化（非目标再强调）

单进程单连接 + 进程内锁（适合本地教学/单机验证）；分区为单列字符串等值分区；
只支持 int64/float64/bool/string；没有压缩小文件、快照过期清理、认证鉴权。
这些场景需要真正的 Iceberg 库与对象存储/目录服务，不在本原型范围内。
