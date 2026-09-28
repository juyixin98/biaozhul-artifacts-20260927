# 简化湖表元数据事务服务（本地、不可变 Parquet）

一个用于教学/评审的**简化**湖表元数据事务服务，数据全部是本地合成
Parquet 文件。明确声明：**这不是完整的 Iceberg 实现**——没有 manifest
list / metadata JSON 文件、没有 schema evolution、没有行级删除向量、没有
catalog 与分布式锁。它只实现并验证以下四件事：

1. **基于快照版本的提交**：每次提交生成一个新快照，快照行集物化在 SQLite
   中，旧快照永不改变（可时间旅行查看）。
2. **互不相交分区的追加可按声明规则重试合并（rebase）**：基于旧快照、但
   只触碰并发提交未改动分区的 APPEND/OVERWRITE 会被重放到新 HEAD 接受，
   响应中 `rebased=true`。
3. **重叠分区覆盖必须检测冲突**：触碰了 base 之后被改动分区的提交一律
   `409 CONFLICT_OVERLAPPING_PARTITION`，绝不 last-writer-wins。
4. **文件先完整写入再挂入快照，失败文件有独立清理记录**：暂存使用
   临时文件 + fsync + 原子 rename；发布是把暂存文件原子 `os.replace` 到
   全新不可变路径；清理台账是**独立的** `cleanup.sqlite3`，每个失败文件
   一行记录。

## 技术栈

Python 3.12 · FastAPI · PyArrow · SQLite（标准库 `sqlite3`，WAL）。
无外部账号、无真实业务数据；所有数据由 `data/fixtures_builder.py` 合成。

## 目录结构（四个有真实职责的模块层）

```
config/
  settings.py            配置层：JSON 文件 + 环境变量，唯一的配置出口
  dev.json               本地默认配置
app/
  adapters/              ① 格式适配层：只懂 Parquet/Schema/分区规范化
    schema.py            规范 schema、兼容性判定
    parquet.py           读取/校验本地 parquet、抽取物理事实、完整暂存拷贝
  kernel/                ② 执行内核 + 元数据事务（与 HTTP 无关）
    errors.py            失败类别枚举、请求 id、脱敏工具
    storage.py           文件暂存/原子发布 + 独立清理台账 CleanupLedger
    metadata.py          SQLite 快照/清单/分区变更/幂等提交日志与冲突规则
  services/
    commits.py           ③ 元数据事务编排：适配→暂存→事务→发布→清理/诊断
    diagnostics.py       结构化 JSONL 诊断（带请求 id，敏感值脱敏）
  api_models.py          ④ 验证接口的 Pydantic 模型
  api.py                 ④ FastAPI 验证接口（薄层，错误映射为稳定 JSON）
  container.py           组装根：settings → 各层对象
tests/                   独立测试：断言具体行集/行数/失败类别
data/
  fixtures_builder.py    合成夹具 + 独立预言 expected.json（len/sum 常量）
  fixtures/              生成的 parquet 夹具与坏文件夹具
examples/                服务调用示例（Python httpx + curl）
docs/                    设计与复现文档
run_results/             运行产物（sqlite、diagnostics.jsonl、测试报告）
```

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock   # 锁定版本，可复现
.venv/bin/python data/fixtures_builder.py    # 生成 data/fixtures 与预言
.venv/bin/python -m pytest                   # 36 个测试
```

启动服务并运行演示：

```bash
.venv/bin/uvicorn app.api:app --port 8077 &
.venv/bin/python examples/run_demo.py        # 四大场景端到端
bash examples/curl_walkthrough.sh            # 等价的 curl 版本
```

## 提交协议（HTTP）

`POST /commits`

```json
{
  "table": "events",
  "operation": "APPEND",            // 或 OVERWRITE
  "request_id": "req-...",         // 幂等键；同 id+同载荷只产生一次结果
  "base_snapshot_id": 3,           // 客户端所基于的快照
  "files": ["/abs/path/a.parquet"]
}
```

冲突/失败响应带稳定类别与请求 id：

```json
{
  "error": "CONFLICT_OVERLAPPING_PARTITION",
  "message": "APPEND conflicts: partition(s) ['region=us/day=2024-01-01'] ...",
  "request_id": "req-b",
  "details": {
    "base_snapshot_id": 1, "head_snapshot_id": 2,
    "overlapping_partitions": ["region=us/day=2024-01-01"],
    "changed_partitions": [...], "requested_partitions": [...]
  }
}
```

失败类别：`VALIDATION`(400)、`CONFLICT_OVERLAPPING_PARTITION`(409)、
`CONFLICT_RETRY_EXHAUSTED`(409)、`STAGING_FAILED`(422)、`NOT_FOUND`(404)、
`INDETERMINATE`(500)。

## 验证矩阵（与 docs/VERIFICATION.md 对应）

| 场景 | 测试 | 断言 |
|---|---|---|
| 并发追加（8 线程，同一 base，分区互不相交） | `test_concurrent_disjoint_appends_all_merge` | 8 个全部提交，最终快照恰 8 文件 8 行，线性快照链 |
| 同分区竞争（5 线程） | `test_concurrent_same_partition_single_winner` | 恰 1 个赢家，4 个输家全部 `CONFLICT_OVERLAPPING_PARTITION`，输家数据从不入快照 |
| 同分区覆盖（陈旧 base） | `test_overlapping_overwrite_is_detected_even_when_files_are_newer` 等 | 409 + 重叠分区明细，HEAD 不变 |
| 提交响应丢失 | `tests/test_idempotency.py` | 同 request_id+同字节重放：同一快照、`replayed=true`、只有 1 个提交/1 个物理文件；被拒绝的结果重放为同一类别 |
| 孤立文件夹具 | `tests/test_orphans.py` / `test_fixtures_oracle.py` | 扫描发现、reconcile 删除且每个文件一行 `WAREHOUSE_ORPHAN/REMOVED`；被旧快照引用的文件不算孤立 |

独立测试不检查“接口能调用”，而是断言**具体行集、行数、失败类别**；
参考答案 `data/fixtures/expected.json` 由手写行的 `len()`/`sum()` 常量构成，
不由被测核心生成。

## 诊断与脱敏

每次接收/接受/拒绝/无法判定都在 `run_results/diagnostics.jsonl` 写一行
JSON，含 `request_id`、`event`、`decision` 与关键状态（base/head 快照、
重叠分区、已发布文件）。敏感键（`owner/email/token/...`）的值以
`<redced:sha256前8位>` 形式输出；测试断言原始邮箱绝不出现于日志。

## 故障语义（为什么接受/拒绝/无法判定）

- **接受**：在一个 `BEGIN IMMEDIATE` 排他事务内读到 HEAD，base→HEAD 链上
  被改分区与本次触碰分区不相交；插入快照+物化清单+分区变更+HEAD 推进+
  提交状态，一个事务原子完成。
- **拒绝（确定性冲突/校验）**：重叠分区、schema 漂移、跨分区 OVERWRITE
  文件等；事务里落一条终态 `REJECTED`，保证重放返回同一结论。
- **无法判定**：物理发布在元数据事务期间失败。元数据回滚，已搬进仓库的
  文件记为 `PARTIAL_PUBLISH_ORPHAN` 并立即按台账清理，返回 `INDETERMINATE`。
- **进程崩溃**：启动时 `recover_pending()` 依据“快照与状态在同一原子事务”
  的不变量，把遗留 `PENDING` 清除（客户端可用同一 request_id 安全重试）；
  暂存文件入台账；仓库中无引用文件由孤立扫描发现。
