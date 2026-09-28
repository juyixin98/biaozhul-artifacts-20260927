# pr2d —— 离线预注册坐标的二维点增量 / 矩形和后端

Rust + Axum + 文件系统 WAL。坐标在建表时一次性**预注册并冻结**；之后只接受
对已注册点的增量更新，按批次**原子发布为不可变版本**；支持任意历史版本的
**闭区间矩形和**查询。

- 坐标压缩顺序固定（排序→去重→升序冻结）；矩形为**闭区间**，边界按排序位置夹取
- 未注册坐标的更新一律 `422` 拒绝，**不就近插入**
- 负增量允许；点累计溢出 `POINT_OVERFLOW`、矩形和溢出 `SUM_OVERFLOW` 都可检测
- 批更新原子发布，查询永远见不到半批；旧版本永久可查；重新建表不影响旧表
- append-only WAL（CRC32 + fsync），启动重放并重算校验，损坏拒绝启动
- 固定版本依赖（`Cargo.toml` 全部 `=` 钉死，提交 `Cargo.lock`）

## 目录结构（按数据格式 / 索引内核 / 持久化 / 接口分层）

```
src/
  error.rs            稳定错误分类 + HTTP 状态映射
  model.rs            PointUpdate / VersionInfo（入参 deny_unknown_fields）
  coord.rs            坐标压缩（冻结轴、rank_leq / rank_lt）
  fenwick.rs          二维 Fenwick 索引内核（i128 累加）
  rect.rs             闭区间矩形语义与容斥
  persist.rs          WAL：定长帧、CRC32、fsync、重放校验
  store.rs            版本化状态机：原子批发布、历史查询、WAL 重放
  config.rs           配置层（env + CLI 覆盖）
  telemetry.rs        run_id / request_id / 日志
  api/                Axum 验证接口（routes / extractor / dto）
  bin/server.rs       入口（优雅关闭；WAL 损坏拒绝启动）
tests/
  hand_cases.rs       手算常量场景（重复坐标/负权/空矩形/极端坐标/时间旅行）
  overflow_atomic.rs  溢出分类、批原子性、陈旧基版本
  version_rebuild.rs  重新建表后旧版本可查 + 重启后续写
  persistence.rs      WAL 截断/位翻转/magic 损坏拒绝启动、重放一致
  crosscheck.rs       固定种子随机交叉验证（对独立稀疏全扫描 Oracle）
  fixture_cases.rs    fixtures/scenarios.json 驱动断言
  http_api.rs         真 TCP 端到端（ureq）：状态码、错误类别、身份头
  common/
    oracle.rs         独立参考实现（HashMap 稀疏映射 + 全扫描，不用内核代码）
    testlog.rs        失败复现 JSONL 日志（run_id/request_id/输入/期望/实际）
    temp.rs           临时目录（PR2D_KEEP_TMP=1 保留现场）
fixtures/scenarios.json  可复用夹具（手算期望值）
scripts/
  verify.sh           一键验证：fmt + clippy(-D warnings) + 全部测试 + 夹具复核 + 冒烟
  check_fixtures.py   独立 Python 稀疏全扫描复核夹具期望值（仅标准库）
  http_smoke.sh       curl 端到端冒烟
docs/
  SEMANTICS.md        边界与版本语义（权威约定）
  API.md              HTTP 接口
```

## 快速开始

```bash
cargo run --bin pr2d-server -- --data-dir ./data --bind 127.0.0.1:8080
```

配置（环境变量 + CLI，后者优先）：`PR2D_DATA_DIR/--data-dir`、
`PR2D_BIND/--bind`、`PR2D_MAX_BODY_BYTES/--max-body-bytes`（默认 1MiB）、
`PR2D_LOG_LEVEL/--log-level`。

```bash
curl -s -XPOST localhost:8080/v1/tables -d '{"xs":[1,2,3],"ys":[10,20]}'
curl -s -XPOST localhost:8080/v1/tables/1/batches \
  -d '{"updates":[{"x":1,"y":10,"delta":5},{"x":3,"y":20,"delta":-2}]}'
curl -s -XPOST localhost:8080/v1/tables/1/query \
  -d '{"x_lo":1,"x_hi":3,"y_lo":10,"y_hi":20}'
```

## 一键验证

```bash
bash scripts/verify.sh
```

依次执行：`cargo fmt --check` → `cargo clippy --all-targets -D warnings` →
全部 Rust 测试 → Python 独立夹具复核 → 真 TCP HTTP 冒烟。

### 期望答案不是“被测实现自己生成的”

三套相互独立的期望来源交叉比对：

1. **手算常量**：`tests/hand_cases.rs` 中每个期望和都在源码里手算写出；
2. **独立 Oracle**：`tests/common/oracle.rs` 是独立的
   `HashMap<(i64,i64),i128>` 稀疏映射 + 矩形全扫描，不调用任何内核代码；
   `tests/crosscheck.rs` 用固定种子随机数据逐版本逐矩形与其比对；
3. **夹具 + Python 复核**：`fixtures/scenarios.json` 的手写期望值由
   `scripts/check_fixtures.py` 用另一份独立稀疏全扫描实现复核，
   `tests/fixture_cases.rs` 再拿这些值核对内核。

### 失败如何复现 / 关联输入与运行身份

- 每次 `cargo test` 有唯一 `run_id`（`testrun-…-pid…`）；失败断言输出
  `run_id`、稳定的 `request_id`、步骤、输入、期望、实际，以及日志目录。
- JSONL 判定日志：`target/pr2d-test-logs/<run-id>/<case>.jsonl`，
  含 `input/expected/actual/verstep/verdict`。
- 保留失败时的数据目录：`PR2D_KEEP_TMP=1 cargo test`（路径会打印出来，
  内含 `wal.log` 可直接解剖）。
- HTTP 响应头 `x-request-id` / `x-run-id` 与服务端日志一致，可交叉检索。
- 失败类别用稳定错误码断言（`COORDINATE_NOT_REGISTERED`、`POINT_OVERFLOW`、
  `STALE_BASE_VERSION`、`CORRUPT_LOG` …），不是只检查“接口能调通”。

## 持久化与崩溃

WAL 记录 `magic|seq|len|JSON|crc32`。每次发布 `fsync`；启动全量重放，
不仅校验 CRC，还**重算每批并与存储格点比对、验证版本链连续**。
截断、位翻转、magic 错误、版本链断裂都导致 `500 CORRUPT_LOG` 并拒绝启动
（见 `tests/persistence.rs`），不会把损坏日志当空库。

## 未执行 / 无法在此环境执行的检查（如实单列，不声称已通过）

- **真实断电持久性**：使用了 `write_all+fsync`（并在建文件时 fsync 目录），
  但没有在真实掉电/磁盘故障硬件上验证；仅能在本环境用“截断/位翻转”模拟介质损坏。
- **高并发性能与锁竞争**：有功能上的乐观并发（`STALE_BASE_VERSION`）测试与
  原子性论证，但没有做高并发吞吐/长尾延迟压测。
- **WAL 增长/压缩与快照落盘**：当前保留完整历史（内存快照 + 仅追加日志），
  未实现日志压缩、快照文件和超大坐标网格的内存上限策略；规模受内存约束。
- 以上均为**已知边界**，相关代码与文档未将其表述为“已验证”。

语义细节见 [`docs/SEMANTICS.md`](docs/SEMANTICS.md)，接口见 [`docs/API.md`](docs/API.md)。
