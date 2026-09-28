# weak-trace-inclusion

两个**有限标号迁移系统（Finite LTS）**之间的**弱迹包含（weak trace inclusion）**检查服务。

* 规格（specification）与实现（implementation）各自声明一组**静默动作（hidden / τ）**；
* 检查问题：**实现可产生的每条可观察迹，规格是否都能接受？**
* 失败时返回“实现可产生、但规格不能接受”的**最短**可观察迹，并附带可逐步回放的证据；
* 状态爆炸等资源耗尽返回 **`unknown`**，绝不与“成立”混淆；
* 判定基于精确的状态**子集对**搜索，**不会**因为两个状态“单步动作看起来一样”就把它们当等价。

技术栈：Rust + Axum 0.8 + Serde。无外部数据依赖，`demo/` 下全部是本地合成夹具。

---

## 1. 快速开始

```bash
# 需要 Rust 1.75+（开发环境为 1.98）
cargo run --release --bin weak_trace_server
# 默认监听 127.0.0.1:8080，可用 --bind 或 BIND_ADDR 覆盖
```

```bash
curl -s http://127.0.0.1:8080/health
curl -s -X POST http://127.0.0.1:8080/check \
  -H 'content-type: application/json' \
  --data-binary @demo/requests/02_counterexample_extra_write.json
```

一键本地演示（自动构建、起服务、发送 5 个场景、落盘响应与日志）：

```bash
./demo/run_demo.sh            # 默认 127.0.0.1:18080，可传参覆盖：./demo/run_demo.sh 127.0.0.1:19000
```

跑全部测试并生成报告 `demo/out/test_report.txt`：

```bash
./demo/run_tests.sh
# 或直接
cargo test
```

## 2. 请求 / 响应契约

### 请求 `POST /check`

```json
{
  "run_id": "可选的客户端运行编号，会原样回显",
  "observable_actions": ["a", "b"],
  "specification": { "LTS" },
  "implementation": { "LTS" },
  "limits": { "可选的资源限制覆盖" }
}
```

`LTS`：

```json
{
  "name": "任意名字",
  "states": ["s0", "s1"],
  "initial_states": ["s0"],
  "hidden_actions": ["tau"],
  "edges": [
    { "id": "可选边id（同-LTS内唯一）", "source": "s0", "action": "tau", "target": "s1" }
  ]
}
```

规则：

* `observable_actions` 是两侧**共用且下标一致**的唯一可观察字母表，不能为空、不能重复；
* 每条边的 `action` 必须么在 `observable_actions` 中，要么在该侧的 `hidden_actions` 中，否则 409；
* 静默动作只在声明它的一侧是 τ；两侧可观察/静默集合重叠是 409；
* `states` 可省略边中出现的状态（自动补登），但 `initial_states` 必须引用已声明状态且非空；
* `limits` 只能把预算**下调**（服务端有硬天花板，见下表默认值即硬上限），给 0 报 400。

| 限制字段 | 含义 | 默认 / 硬上限 |
| --- | --- | --- |
| `max_states_per_lts` | 单侧状态数 | 500 000 |
| `max_edges_per_lts` | 单侧边数 | 2 000 000 |
| `max_alphabet` | 可观察字母表大小 | 4 096 |
| `max_closure_pairs` | 闭包/弱像可达二元组工作量 | 2 000 000 |
| `max_search_nodes` | 子集对 BFS 访问节点数 | 100 000 |

请求体硬上限 64 MiB。

### 判定结论（HTTP 始终 200，结论看 `verdict`）

* **`included`**：实现的弱迹语言包含于规格；
* **`counterexample`**：最短反例。字段：
  * `counterexample.trace`：可观察动作名序列；`shortest: true`；
  * `counterexample.implementation_reachable` / `specification_reachable`：沿该迹两侧的末态全集（后者必须为空）；
  * `evidence_report`：**独立验证器**（实现见 `src/evidence.rs`，不复用求解器内部表）重新模拟两侧，并抽取一条实现侧的具体路径 `impl_witness`（初态、观察前 τ 段、观察边、观察后 τ 段、末态），逐边审计源/目标/动作一致性；
* **`unknown`**：资源预算耗尽。`unknown.reason_code ∈ {closure_pair_limit, search_node_limit}`，并明确声明“不得解释为成立”。

所有成功响应都带：`run_id`（服务端 UUID v4）、`client_run_id`（回显）、`elapsed_ms`、`diagnostics`（状态/边数、闭包二元组数、弱像二元组数、已访问搜索节点、层数、最大层宽、生效限制）。

### 错误（HTTP 非 200）与错误语义

四类错误**可区分**，响应体形如：

```json
{ "run_id": "...", "category": "state_conflict", "code": "unknown_action",
  "message": "...", "details": ["..."] }
```

| category | HTTP | code |
| --- | --- | --- |
| `input_error` | 400 | `malformed_json`, `invalid_request_shape`, `empty_observable_alphabet`, `invalid_limit_value`, `missing_field`, `invalid_action_name`, `empty_state_name`, `empty_edge_id`, `duplicate_observable_action` |
| `state_conflict` | 409 | `duplicate_state`, `duplicate_edge_id`, `unknown_state`, `unknown_action`, `duplicate_hidden_action`, `observable_hidden_overlap`, `initial_states_empty`, `no_initial_state` |
| `payload_too_large` | 413 | `body_too_large`, `hard_limit_exceeded` |
| `computation_failure` | 500 | `solver_failure`（求解器 panic 等内部问题，绝不伪装成成立/未知） |

注意语义边界：**资源耗尽不是错误**（HTTP 200 + `verdict: unknown`）；输入错误与系统内部状态冲突分别是 400 / 409。

## 3. 算法

代码在 `src/solver.rs`。给定 LTS 与声明的 τ 边：

1. **静默闭包并保留可回放映射**：对每个状态 `s` 用 DFS 求
   `C(s) = { t | s ==τ⇒ t }`；每个可达二元组 `(s,t)` 记录 `(父状态, τ 边索引)`，
   因此任何 `s ==τ⇒ t` 都能重放出一条具体 τ 路径（`ClosureInfo::replay_path`，有单元测试）。
2. **弱像**：`Img_a(X) = C({ t | ∃s∈X, s --a--> t })`，即“先可观察 a，再任意 τ”，预计算到状态。
3. **子集对分层 BFS**：节点是 `(I, S)`——实现经迹 `w` 弱可达的状态集 I、规格经同一 `w` 弱可达的状态集 S。
   根节点为 `(C(I0), C(S0))`；沿字母表升序逐层扩展。第一个 `I ≠ ∅ 且 S = ∅` 的节点即反例。
   * 分层 BFS + 动作升序 ⇒ **长度最短、同长度字典序最小**；
   * 节点去重键是两个**精确状态集**，不是“一步出边签名”，因此不会把不同到达态错误合并
     （`tests/core_semantics.rs::equal_one_step_signatures_must_not_be_merged` 专门钉住这一点）。
4. 闭包二元组 / 弱像二元组 / BFS 节点任一项超预算 ⇒ `unknown`（已完成的搜索量仍写进诊断）。

**独立证据验证**（`src/evidence.rs`）：另一套 τ 闭包/弱像实现（名字集合视角，代码路径刻意不同）
重放反例迹，断言“实现接受、规格拒绝”，并用后向可行集抽出一条具体路径，再逐边审计。
测试里的**参考答案预言机**（`tests/core_semantics.rs`）同样独立于被测内核：
朴素短迹穷举（BTreeSet + 队列），逐迹对照求解器结论。

## 4. 模块边界

```
src/
  error.rs     统一错误契约：ErrorCode / ErrorCategory / HTTP 映射
  model.rs     线缆 JSON 类型 + 索引化内部表示（Lts / Edge / EdgeLabel）
  input.rs     输入语言：校验、名字索引化、可观察字母表对齐
  solver.rs    求解内核：τ 闭包(含回放映射)、弱像、子集对 BFS、资源预算
  evidence.rs  独立证据验证：重放反例迹、抽取并审计具体路径
  service.rs   Axum 后端：/health、/check、分类错误响应、带 run_id 的诊断日志
  lib.rs       库导出
  main.rs      服务入口 weak_trace_server
tests/         35 个测试（内核语义 / 输入契约 / 证据验证 / HTTP 集成）
demo/          合成夹具、演示与测试脚本、运行产物
```

模块间只通过 `error::Result` / `model` 中具名类型传递数据与错误。

## 5. 测试与诊断

```bash
cargo test                 # 单元 2 + 集成 33
cargo clippy --all-targets # 零警告
```

覆盖的关键场景（断言具体结果与失败类别，不止“接口能调用”）：

* **隐藏内部步骤**：规格多一个内部 `tick`/`reset` 不改变可观察语言 ⇒ included，独立穷举到长度 6 对照；
* **错误额外输出**：实现登录后多出 `write` ⇒ 最短反例精确为 `[login, write]`（不是 `[write]`，初始态做不到）；
* **不可达分支**：死状态上的 `eject` 边不构成反例 ⇒ included，并显式断言实现侧从初态做不到 `eject`；
* **不按一步签名合并**：两个实现状态出边标签相同但后缀行为不同，反例 `[a,a,x]` 必须被找到；
* **状态爆炸**：真实答案其实是 included，但小预算先耗尽 ⇒ `unknown/search_node_limit`（以及 `closure_pair_limit` 用例）；
* **错误分类**：空字母表 400、悬空初态 409、未知动作 409、超大 413、畸形 JSON 400 等逐码断言；
* **证据防伪**：对 60 个随机小 LTS，凡求解器给反例，独立验证器必须 accepted；规格能接受的迹/实现做不到的迹必须被验证器拒绝。

每次请求的日志（`demo/out/server.log`）保留：**运行编号（`run_id` + 自增 `seq`）**、
两侧系统名与字母表大小、判定、反例迹与证据是否通过、节点数/层数等**关键中间状态**、
以及拒绝/中止的**判断理由**。响应体（`demo/out/*.response.json`）与日志可用同一个 `run_id` 串起重放。

## 6. 复现一次问题的最短步骤

```bash
./demo/run_demo.sh
# 1) 在终端输出里找到场景与 HTTP 状态；
# 2) 打开 demo/out/02_counterexample_extra_write.response.json 看 trace / evidence_report；
# 3) 用响应里的 run_id 在 demo/out/server.log 检索：grep <run_id> demo/out/server.log
# 4) 修改 demo/requests/*.json 后重跑脚本即可复现自己的用例。
```
