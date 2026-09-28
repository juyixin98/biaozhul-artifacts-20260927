# 复现文档 — btmon 有界时序规则监视器

本文档给出从干净检出到全部结果可复核的完整步骤。所有数据都是本地合成夹具，
无外部账号、无网络业务依赖；构建时只从 crates.io 拉取 Rust 依赖。

- 工具链：Rust / cargo 1.98（2021 edition）
- 依赖：axum 0.8、tokio 1、serde 1、serde_json 1、uuid 1、sha2 0.10、hex 0.4
- 依赖锁定：`Cargo.lock` 已提交，`cargo build --locked` 可复现相同版本

## 1. 构建

```bash
cargo build --locked --release
# 二进制：target/release/btmon
```

## 2. 离线展开求值（不启动服务）

`btmon replay <spec.json> [out.jsonl]` 对同一条轨迹执行**两套独立算法**：

1. 增量内核（`src/monitor.rs`）：逐步维护义务表与哈希链决策日志；
2. 参考答案（`src/reference.rs`）：集合式朴素重扫——枚举全部触发实例，
   对每个实例独立扫描存在/全称窗口。两者只共享输入语言类型，算法无共享。

退出码 `0` 表示打开态与封闭态的逐字段交叉核对全部一致；不一致时退出码 `2`。

```bash
# 单个夹具
./target/release/btmon replay fixtures/runs/01-overlap-all.json /tmp/01.jsonl

# 全部夹具（结果写入 results/replay/）
for f in fixtures/runs/*.json; do
  n=$(basename "$f" .json)
  ./target/release/btmon replay "$f" "results/replay/$n.jsonl"
done
```

每条输出 JSONL 含：`run_id`、`cross_check_open/closed`、内核与 oracle 的
义务表（含窗口、状态、理由码、满足步/失败步）、完整决策日志 `journal`、
按规则汇总 `rules`。这些就是"离线展开求值"的可复核证据。

## 3. 手算夹具与期望

- `fixtures/runs/*.json`：11 个手算轨迹（含 `open_checks` 前缀裁决检查）。
- `fixtures/expected/*.json`：每个义务实例的**手算**期望（状态、理由码、
  窗口边界、满足/失败步），不来自被测核心。

| 夹具 | 覆盖点 | 打开态 | 封闭态 |
|---|---|---|---|
| 01-overlap-all | 重叠触发，一个响应满足多个义务（satisfy=all） | sat | sat |
| 02-boundary | 同步/窗口首步/窗口末步响应，真·截止错过 | viol | viol |
| 03-missing-early | 提前结束、缺响应、strict | **wait** | viol(closed_pending) |
| 04-lenient | 同 03 但 lenient | **wait** | sat(closed_accepted) |
| 05-sustain-fail | 保持窗口中途条件失败 | viol | viol(condition_failed) |
| 06-sustain-incomplete | 保持窗口未观测完整就结束 | **wait** | viol(closed_incomplete) |
| 07-satisfy-one-ok | satisfy=one 不重叠窗口逐一配对 | sat | sat |
| 08-satisfy-one-miss | 重叠窗口 one 模式只消费最早义务 | viol | viol(deadline_missed) |
| 09-rotation | 版本旋转封闭旧 epoch，旧义务不匹配新事件 | viol | viol |
| 10-mixed | 响应+保持+合取原子混合，多结果并存 | viol | viol |
| 11-overlap-all-control | 与 08 同轨迹的 satisfy=all 对照 | sat | sat |

## 4. 自动化测试（断言具体结果与失败类别）

```bash
cargo test --locked
```

- `tests/fixtures.rs`（13）：手算期望逐项断言 + 前缀三值 + wait 不得当通过。
- `tests/crosscheck.rs`（5）：约 680 条确定性随机轨迹，内核 vs 独立 oracle
  逐字段核对（all/one × strict/lenient × 打开/封闭 × 旋转切点）。
- `tests/errors.rs`（12）：四类错误的 code/category/HTTP 状态码：
  输入 `input/400`、状态冲突 `state/409`、资源耗尽 `resource/507`、
  计算失败 `computation/500`，外加 `not_found/404`。
- `tests/evidence.rs`（11）：快照摘要、哈希链、篡改检测、恢复后续跑一致、
  版本不混用、epoch 标签。
- `tests/http.rs`（13）：真实 Axum router（oneshot，无 socket）端到端。

测试运行编号（run id）与关键中间状态以 JSONL 追加到
`target/replay/integration.jsonl`（每步：run_id、epoch、spawned/satisfied/
violated、全局裁决与理由），可用该日志重放问题。

## 5. HTTP 服务真实调用（正常 + 异常）

```bash
BTMON_BIND=127.0.0.1:8080 ./target/release/btmon serve
# 另一个终端：
./examples/curl-walkthrough.sh
```

脚本对 19 组调用断言 33 项（状态码与错误码），响应全量落到
`results/http-walkthrough.log`，断言统计在 `results/walkthrough.stdout`。
已实际运行并保留结果：`== assertions: pass=33 fail=0 ==`。

## 6. 已保留的运行结果（本仓库内）

- `results/cargo-test.log`：`cargo test --release` 完整输出（54 passed, 0 failed）。
- `results/replay/*.jsonl`：11 个夹具的离线双算法核对结果。
- `results/http-walkthrough.log`：真实 HTTP 正常/异常调用的完整响应。
- `results/walkthrough.stdout`：33 项断言统计。
- `results/server.log`：服务启动日志。
- `target/replay/integration.jsonl`：测试生成的可重放中间状态日志
  （由 `cargo test` 重新生成）。

## 7. 异常分类速查（错误体形状统一）

```json
{ "error": { "code": "STEP_GAP", "category": "input",
             "message": "non-contiguous step: expected 1, got 2",
             "run_id": "run-..." } }
```

| category | HTTP | 代表 code |
|---|---|---|
| input | 400 | MALFORMED_JSON, EMPTY_RULESET, BAD_WINDOW, DUPLICATE_RULE_ID, EMPTY_EVENT, STEP_GAP, SAME_VERSION, BODY_TOO_LARGE |
| state | 409 | MONITOR_CLOSED, STEP_REGRESSED, VERSION_MISMATCH, MONITOR_EXISTS, SNAPSHOT_DIGEST_MISMATCH, DECISION_CHAIN_BROKEN, SNAPSHOT_ID_MISMATCH |
| resource | 507 | OBLIGATION_LIMIT, EPOCH_LIMIT, DECISION_LOG_LIMIT |
| computation | 500 | COMPUTATION_FAILED（快照内部不一致等） |
| not_found | 404 | NOT_FOUND |

所有请求/响应都带 `x-run-id`（缺省自动生成 UUID），服务端把它写进决策日志，
错误体也回传它，便于按编号重放。
