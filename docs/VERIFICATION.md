# 验证与复现（可复核结果）

本文件记录四类必验场景的**断言点**与一次真实运行的留档结果。所有产物在
`run_results/`：`pytest_output.txt`、`pytest_report.xml`、`demo_output.txt`、
`curl_output.txt`、`abnormal_responses.txt`、`cleanup_records.json`、
`commit_log.json`、`diagnostics.jsonl`、`server.log`。

## 0. 从零复现

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python data/fixtures_builder.py          # 生成合成夹具 + expected.json
.venv/bin/python -m pytest                          # 39 个独立断言测试

# 真实 HTTP 运行
.venv/bin/uvicorn app.api:app --port 8077 &
.venv/bin/python examples/run_demo.py              # 四场景端到端
bash examples/curl_walkthrough.sh                  # 等价 curl 版
```

最近一次留档：**39 passed**（`run_results/pytest_report.xml`），
并发测试连续多轮重复运行无竞态失败。

## 1. 并发追加（互不相交分区）

- 测试：`tests/test_concurrency.py::test_concurrent_disjoint_appends_all_merge`
  ——8 个真实 OS 线程经 Barrier 同时从 base=1 追加 8 个互不相交分区。
- 断言（具体结果，非“能调用”）：
  0 个错误；最终 HEAD 恰含 8 个文件、每个 1 行；快照链恰为 ROOT+8；
  8 个结果全为 COMMITTED。
- 真实运行（`demo_output.txt` 场景 1）：us 与 eu 两个并发请求都提交，
  后到者响应 `"rebased": true`，其父快照挂在先到者快照上。

## 2. 同分区覆盖 / 竞争（必须冲突，禁止最后写获胜）

- 顺序覆盖：`test_overlapping_overwrite_is_detected_even_when_files_are_newer`、
  `test_overwrite_replaces_exactly_target_partition`。后者断言覆盖后行集
  恰为 `{b,c,v2}` 三个文件、行数 2+1+1，旧分区在新快照消失但**旧快照仍含
  a.parquet**（不可变、可时间旅行）。
- 并发同分区：`test_concurrent_same_partition_single_winner`——5 线程同压
  us/0101，断言**恰 1 个赢家**、其余 4 个全部
  `CONFLICT_OVERLAPPING_PARTITION`，输家文件从不进入任何快照。
- 真实运行（`demo_output.txt` 场景 2b、`abnormal_responses.txt`）：

  ```json
  HTTP 409
  {"error":"CONFLICT_OVERLAPPING_PARTITION","request_id":"req-demo-ovw-stale",
   "details":{"base_snapshot_id":1,"head_snapshot_id":4,
              "overlapping_partitions":["region=us/day=2024-01-01"], ...}}
  ```

  提交日志中 `req-demo-ovw-stale = REJECTED`，HEAD 未被推进（非最后写获胜）。

## 3. 提交响应丢失（幂等重放）

- 成功重放：`tests/test_idempotency.py::test_successful_commit_is_replayed_not_recommitted`
  ——同一 request_id + 同字节重发：`replayed=true`、同一 snapshot_id、
  快照数仍为 ROOT+1、仓库物理文件仍为 1、提交行仍为 1。
- 拒绝重放：`test_rejected_commit_replays_same_category`——被冲突拒绝的
  请求重放仍返回 `CONFLICT_OVERLAPPING_PARTITION`（`details.replayed=true`），
  不会因 rebase 而悄悄成功。
- 坏文件重放：`test_unreadable_file_failure_carries_request_id_and_replays_same_category`
  ——不可读文件首次 422 带调用方 request_id；重放返回同一类别且
  `replayed=true`，不产生快照。
- 真实运行（`abnormal_responses.txt` A/B）：两次提交同一坏文件，第二次
  响应 `"details":{"replayed":true}`，类别仍为 STAGING_FAILED。

## 4. 孤立文件夹具

- `test_unrelated_orphan_directory_detected_and_removed`：在仓库下植入
  与任何表无关的 `_orphan_demo/stray.parquet`，扫描报告
  `_orphan_demo/stray.parquet`，reconcile 后目录/文件消失，且清理台账
  逐文件一行 `(WAREHOUSE_ORPHAN, REMOVED)`。
- `test_overwritten_file_is_not_orphan_while_old_snapshot_exists`：
  OVERWRITE 后旧文件仍被旧快照引用，`scan_orphans()` 必须为空——不得
  误删可时间旅行的文件。
- 真实运行（`demo_output.txt` 场景 4）：扫描→删除闭环；本次留档台账：

  ```
  7 records: {('STALE_STAGED_FILE','REMOVED'): 5,
              ('WAREHOUSE_ORPHAN','REMOVED'): 2}
  ```

## 5. 失败类别总表（独立测试均断言类别，而非仅状态码）

| 类别 | HTTP | 触发与断言位置 |
|---|---|---|
| VALIDATION | 400 | schema 漂移、跨分区 OVERWRITE 文件、request_id 复用不同载荷 |
| CONFLICT_OVERLAPPING_PARTITION | 409 | 陈旧 base 触碰被改分区（含并发同分区 4 个输家） |
| CONFLICT_RETRY_EXHAUSTED | 409 | 元数据写锁重试预算耗尽 |
| STAGING_FAILED | 422 | 非 parquet / 文件缺失，带 request_id，可幂等重放 |
| NOT_FOUND | 404 | 未知表 / 未知快照 |
| INDETERMINATE | 500 | 元数据事务内物理发布失败（已发布文件逐笔入台账清理） |

## 6. 诊断与脱敏（为什么接受/拒绝/无法判定）

`run_results/diagnostics.jsonl` 每行含 `request_id / event / decision /
reason / state`。最近一次留档事件计数：

```
ACCEPTED 10 · REJECTED 4 · INFO 11
commit.committed 5 · commit.replayed 3 · commit.rejected 2 ·
commit.validation_failed 1 · commit.replayed_prestage_failure 1 ·
orphan.reconciled 1
```

- 接受的 state 带 base/head 快照、rebased、挂载文件与分区。
- 拒绝的 state 带 base/head、overlapping/changed/requested 分区。
- 脱敏：敏感键（owner/email/token/password/secret/user）输出
  `<redacted:sha256前8位>`；`test_redact_replaces_sensitive_scalars_but_keeps_structure`
  直接断言原值被替换、非敏感结构保留、同值标记稳定；端到端测试断言原始
  邮箱在日志中出现 0 次。

## 7. 独立预言（答案不由被测核心生成）

`data/fixtures_builder.py` 手写行集，`expected.json` 的期望行数是
`len(rows)`、金额和是 `sum(...)` 常量；`test_fixtures_oracle.py` 还会
直接用 PyArrow 读回**已发布的物理文件**重算 amount_sum 与预言比对，
不经过本服务的元数据。

## 8. 这不是完整 Iceberg 实现

无 manifest list / metadata.json 元数据格式、无 catalog、无 schema
evolution、无行级删除（position/equality delete files）、无对象存储与
跨进程提交协调、分区仅用规范化字面量而非 Iceberg transform。范围严格
限定于：快照物化、声明式分区冲突/rebase、原子挂载与独立清理台账。
