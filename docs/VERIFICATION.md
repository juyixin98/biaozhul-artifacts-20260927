# 验证记录（VERIFICATION）

本文件如实记录一次从干净工作树执行的验证结果。环境：

- OS：Linux 6.8.0-90-generic (x86_64)
- 工具链：rustc 1.98.1 (48a229cea 2026-09-01)，cargo 1.98.1
- 日期：2026-09-27
- 依赖：首次 `cargo build` 从 crates.io 拉取（axum 0.7、tokio 1、serde 1、
  serde_json 1、toml 0.8、thiserror 1、tower 0.5、tower-http 0.6、
  tracing 0.1、tracing-subscriber 0.3、uuid 1），版本以 `Cargo.lock` 为准。

## 1. 构建与静态检查

```
$ cargo build --release
    Finished `release` profile [optimized + debuginfo] target(s)
$ cargo clippy --all-targets
（0 warning / 0 error）
```

## 2. 测试结果（`cargo test`）

```
Running unittests src/lib.rs
test result: ok. 30 passed; 0 failed; 0 ignored

Running tests/api.rs
test result: ok. 13 passed; 0 failed; 0 ignored

Running tests/exhaustive_cross_check.rs
test result: ok. 2 passed; 0 failed; 0 ignored

Running tests/fixtures_models.rs
test result: ok. 3 passed; 0 failed; 0 ignored
```

合计 **48 个测试全部通过**。覆盖：

- 发射语义：全输入弧同时检查、容量溢出阻断且不截断、原子消耗/生成、自环净增量、
  非法参数与“合法但阻断”分类。
- P 不变量：关联矩阵秩/零空间维数、本原整数基、带权弧上的守恒律 `[1,2]`、
  零空间为 0 时无候选、非守恒向量残差非零。
- 可达性：最短路、容量穷尽判不可达、`state_limit` 截断判 inconclusive、
  不变量零搜索否决。
- 输入层：9 类稳定错误码（空网、重名、负容量、0 权弧、未知库所、超容量、长度不符、
  非法 JSON、未知字段），并验证多问题一次收集。
- `.pnet`：注释、空 consumes、行列号诊断、语义错误透传、带权弧。
- 证据验证：独立重放合法/输入缺口/容量溢出/未知变迁、守恒残差与加权和、形状错误分类。
- 配置：默认值、按键覆盖、坏 TOML 报错而非静默。
- HTTP：13 个端到端，断言具体判定、具体序列、具体 HTTP 码与 `error` 码、422 失败类别、
  request-id 回显/生成、413 体积限制、`.pnet` 提交。

## 3. 独立性说明

可达真值由 `tests/common/mod.rs` 的独立参考实现生成：

- 用 `BTreeSet<Vec<i64>>` 递归穷举容量合法可达集（与内核的 `HashMap`+BFS 完全不同的写法）；
- 独立 BFS 计算参考最短路径；
- **不 import 任何内核求解函数**。

`exhaustive_cross_check` 对三个夹具的**全部容量合法标识**逐一调用内核 BFS 与参考真值对照：
mutex 2^5=32、producer_consumer 6·2·2·5=120、deadlock 2^8=256 个笛卡尔标识
（可达子集更小）。对每个可达目标还做：

1. 内核证据序列由 `verify::replay`（第二套独立发射实现）逐步重放，断言终点精确一致；
2. 参考实现给出自己的最短路径，再由 `verify::replay` 重放；
3. 对每个已验证残差为 0 的守恒律，断言路径上每个标识加权和恒定（令牌不变量）；
4. 断言内核路径长度 ≤ 参考最短路长度；
5. 不可达目标断言无伪造证书，且 `basis` 必须是 `capacity_state_space_exhausted`
   或 `p_invariant_conservation_witness`；
6. 参考可达集的每个标识都满足容量边界（发射从不截断）。

## 4. 端到端会话（真实服务，真实输出）

启动：`RUST_LOG=info PETRI_SERVER_PORT=8091 ./target/release/petri-reach`

```
### reachable（互斥，A 入临界区）
{"decision":"reachable","reachable":true,"basis":"bfs_explicit_path",
 "seq":["enter_a"],"states_visited":2,"request_id":"doc-1"}

### unreachable（两进程同时在临界区，资源守恒律否决）
{"decision":"unreachable","basis":"p_invariant_conservation_witness",
 "obs":{"weights":[1,0,1,0,1],"initial_weighted_sum":1,
        "target_weighted_sum":2,"residual_max_abs":0},"request_id":"doc-2"}

### producer/consumer，buffer=5（恰好到容量上界）
{"decision":"reachable","seq":["produce","produce","consume","produce","produce"]}

### state_limit 截断
{"decision":"inconclusive","basis":"state_limit_reached","states_visited":5}

### 容量溢出证据 -> 422（b: 4+2=6 > capacity 5）
422
{"error":"firing_evidence_invalid",
 "overflows":[{"capacity":5,"place":"b","resulting":6}]}
```

日志按 request-id 关联，并显示版本、状态空间上界与判定依据：

```
INFO petri_reach: starting petri-reach service="petri-reach" version="0.1.0"
     bind=127.0.0.1:8091 state_limit=200000 coefficient_bound=4
INFO bfs{request_id=doc-1}: start reachability BFS state_space_upper_bound=32 ...
INFO bfs{request_id=doc-1}: target reachable path_length=1 visited_count=2
INFO ... reachability analysis complete request_id=doc-1 decision="reachable" ...
INFO bfs{request_id=doc-2}: reachability rejected by P-invariant conservation witness
     candidates=8 truncated=false weighted_initial=1 weighted_target=2
```

## 5. 已知边界与如实声明

- `unreachable` 仅在**显式容量模型内**完备；不外推为无界网完整可达判定（每个响应 `scope` 声明）。
- P 不变量候选为**有界枚举**（默认系数 l1 ≤ 4）；`candidates_truncated=true`
  表示可能遗漏更大系数候选。但快速否决只采用单个残差已验证为 0 的守恒见证，故否决严格。
- BFS 触达 `state_limit` 返回 `inconclusive`，不会伪装成不可达或成功。
- 调试时注意：若环境预置了全局 `RUST_LOG`（本机预置 `RUST_LOG=warn`），它会按 tracing
  约定优先于配置 `log_level`；需要请求级日志时显式 `RUST_LOG=info`（或 `debug`）。
