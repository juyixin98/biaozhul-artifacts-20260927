# 有限轨迹上的有界时序规则监视器（Bounded Temporal Rule Monitor）

在有限轨迹（finite trace）上监视两类有界时序义务，并给出三值结论：

- **响应义务 respond-within-N**：事件 `trigger` 出现后，`within_steps` 个后续步内
  必须出现匹配的 `response` 事件。
- **保持义务 hold-for-K**：`always`（全程）或每次 `trigger` 之后，条件必须连续
  保持 `duration_steps` 步。

三值结果（LTL3 风格）：

| 结论 | 含义 |
|---|---|
| `satisfied` | 轨迹已封闭（end 标记）且无违反 |
| `violated` | 已有义务违反（截止过期、保持中断、或封闭时仍悬而未决） |
| `pending` | 轨迹尚未结束；**等待绝不等于通过** |

技术栈：Rust + Axum 0.7 + Serde（无数据库、无外部服务，全部数据为本地合成夹具）。

---

## 1. 工程结构

模块间只通过明确的数据结构与四类错误（见下）通信，内核不依赖 HTTP 层：

```
src/
  error.rs       错误契约：input_error / state_conflict /
                 resource_exhausted / computation_failed
  language.rs    输入语言 DTO：Ruleset / RuleDef / Predicate / Step + 校验
  matcher.rs     谓词与事件模式求值（内核与 oracle 共用的唯一求值原语）
  kernel.rs      在线监视内核：义务实例、事务式步进、三值判定、快照/恢复
  oracle.rs      独立离线"展开（unfold）"参考实现，不调用内核任何状态机代码
  evidence.rs    证据打包与校验（oracle 全量重放 + 内核重驱动 + 快照恢复）
  store.rs       内存监视器注册表
  api.rs         Axum HTTP 后端（run_id 信封、错误码→HTTP 状态映射）
  bin/cli.rs     CLI：serve / online / offline / verify
fixtures/
  rulesets/      合成规则集 shop-v1.json
  traces/        a–g 七条手算轨迹
  expected/      每条轨迹手算的义务集合与判定理由
tests/           夹具交叉核对、内核语义、错误分类、证据、HTTP 集成
examples/        http-smoke.sh、cli-demo.sh
test_artifacts/  测试产生的 JSONL 重放日志与冒烟留档（可复核）
```

### 关键语义约定

1. **多个触发分别追踪**：同一规则每次触发生成独立义务，确定性 id 为
   `<rule_id>#o<ordinal>`（ordinal 按规则内触发顺序从 0 递增）。
2. **一个响应满足哪些义务由 `consume` 策略明确决定**：
   - `all_matching`（默认）：满足该规则下所有挂起且匹配（含相关键）的义务；
   - `earliest_deadline`：仅满足截止最早的一个（再按触发步、id 打破平局）。
3. **相关键 correlation_key**：声明后，响应事件必须携带与触发事件相同的键值，
   事件类型匹配但键值不同不解除义务。
4. **截止边界**：触发于 t、窗口 N，则截止步为 `t+N`；响应恰在截止步出现**算满足**；
   截止步结束时仍无匹配响应则该步违反（`deadline_expired`，违反步=截止步）。
5. **结束标记封闭剩余义务**：`end: true` 步封闭有限轨迹，所有仍 pending 的响应义务
   与未完成的触发式保持窗口判违反（`trace_closed_unresolved` /
   `trace_closed_incomplete`）。裸 end 标记（不带事件）不占观测事件。
6. **always 保持规则**：零观测事件的空轨迹真空满足（`vacuously_held_empty_trace`）；
   非空轨迹必须观测到至少 K 个事件且每步条件成立，否则
   `held_throughout` 满足或 `trace_too_short_for_duration` / `condition_broke` 违反。
7. **版本隔离**：快照记录规则集 `id` + `version` + 规范 JSON 的内容哈希。
   用不同版本、或同版本但内容被修改的规则集恢复快照一律拒绝
   （`ruleset_version_mismatch` / `ruleset_content_mismatch`），旧规则状态不可能
   与新规则混用。
8. **步进是事务性的**：先完成所有可能失败的求值（阶段 A），再一次性提交状态
   （阶段 B）。任何输入/计算/资源错误都不会让监视器停留在半更新状态。

### 四类可区分错误

| `error_kind` | HTTP | 典型 reason |
|---|---|---|
| `input_error` | 400 | `malformed_json`, `zero_window`, `duplicate_rule_id`, `empty_and`, `predicate_too_deep` |
| `state_conflict` | 409 | `step_index_gap`, `step_index_duplicate_or_reordered`, `monitor_closed`, `unknown_monitor`, `ruleset_version_mismatch`, `ruleset_content_mismatch` |
| `resource_exhausted` | 422 | `max_steps_exceeded`, `max_obligations_total_exceeded`, `max_obligations_per_step_exceeded`, `max_monitors_exceeded` |
| `computation_failed` | 422 | `non_numeric_fact`, `non_boolean_fact`, `missing_correlation_fact` |

默认资源上限见 `src/kernel.rs` 的 `Limits::default`，创建监视器时可在请求里覆盖。

---

## 2. 构建与复现

需要 Rust（开发环境为 rustc/cargo 1.98）。依赖已锁定在 `Cargo.lock`。

```bash
cargo test --release          # 全部 30 个测试（含交叉核对与 HTTP 集成）
cargo clippy --all-targets    # 0 warning
cargo build --release         # 生成 target/release/bounded-monitor
```

只跑测试看结果摘要：

```bash
cargo test 2>&1 | grep "test result"
# api 5 / error_classes 4 / evidence 5 / fixtures 7 / kernel_semantics 9
```

### CLI

```bash
# 在线内核（逐步打印 spawned/resolved/violated 与当前判定）
cargo run --release -- online  fixtures/rulesets/shop-v1.json <(
  python3 -c "import json;d=json.load(open('fixtures/traces/a_boundary_satisfied.json'));print(json.dumps({'steps':d['steps']}))")

# 独立离线展开参考实现
cargo run --release -- offline fixtures/rulesets/shop-v1.json <(
  python3 -c "import json;d=json.load(open('fixtures/traces/b_overlap_triggers.json'));print(json.dumps({'steps':d['steps']}))")
# 退出码：0 satisfied/pending，1 violated，2 输入/用法错误
```

一键示例：`./examples/cli-demo.sh`（正常、违反、坏输入三种退出码）。

### HTTP 服务

```bash
cargo run --release -- serve 127.0.0.1:8080
# 另一个终端：
./examples/http-smoke.sh                 # BASE 可覆盖，默认 127.0.0.1:8080
```

所有响应都是 `{"run_id": ..., "data": ...}` 或
`{"run_id": ..., "error_kind": ..., "reason": ..., "detail": ...}` 信封，
并在响应头回显/生成 `x-run-id`。

端点：

| 方法与路径 | 作用 |
|---|---|
| `GET  /health` | 健康检查 |
| `POST /monitors` | `{monitor_id?, ruleset, limits?}` 创建监视器 |
| `GET  /monitors/:id` | 当前判定 + 全部义务实例 |
| `POST /monitors/:id/steps` | 提交一个步（index 必须从 0 连续递增） |
| `POST /monitors/:id/close` | 追加裸 end 标记并封闭轨迹 |
| `GET  /monitors/:id/snapshot` | 取可恢复快照 |
| `GET  /monitors/:id/evidence?snapshot_after=N` | 取可重放证据（可带切口） |
| `POST /restore` | `{monitor_id?, ruleset, snapshot}` 恢复为新监视器 |
| `POST /evaluate` | 一次性离线 oracle 求值 `{ruleset, trace}` |
| `POST /verify` | 校验证据信封内的 `data`（证据本体） |

---

## 3. 验证方案：手算 ↔ 内核 ↔ 独立 oracle

参考答案**不是**由被测核心自己生成的：

1. **先在纸上手算**每条轨迹的义务集合，写入 `fixtures/expected/*.json`
   （含每个义务的触发步、截止步、满足步、违反步、判定理由）。
2. 每个夹具测试同时断言三方一致：
   - 手算期望；
   - 在线内核 `kernel.rs`（逐步驱动）；
   - 独立离线 `oracle.rs`（只共享语言类型和谓词求值，义务簿记是另写的一份）。
3. 证据校验 (`tests/evidence.rs`) 额外验证：
   - 真实证据通过；
   - 篡改声称判定 → `claimed_verdict_vs_oracle`；
   - 篡改义务状态 → `obligations_at_cut`；
   - 用另一版本规则集恢复 → 恢复阶段 `state_conflict`，证据校验出现
     `snapshot_ruleset_version` / `snapshot_ruleset_hash`；
   - **恢复一致性**：在切口 N 快照 → 恢复 → 只驱动后缀，最终判定与义务集合
     必须与从不重启的运行逐字段相同；
   - 切口快照必须等于在同一切点重新计算的快照（`snapshot_determinism`）。

覆盖的手算场景：

| 夹具 | 场景 | 最终判定 |
|---|---|---|
| a | 边界响应（响应落在窗口内）、单触发多义务、always 恰好 K 事件 | satisfied |
| b | 重叠触发、相关键错配不解除义务、earliest_deadline 只消一个、提前截止/封闭并存 | violated |
| c | 三个响应全部缺响应，各自在截止步过期；`exists` 谓词把关响应模式 | violated |
| d | 提前封闭：裸 end 标记封闭全部挂起义务；always 观测事件不足 K | violated |
| e | 重叠保持窗口，后一窗口在条件中断步违反 | violated |
| f | 两个重叠保持窗口都在各自最后一个要求步精确完成 | satisfied |
| g | 空轨迹裸封闭，always 真空满足 | satisfied |

另外 `tests/kernel_semantics.rs` 有 9 个代码级用例，包括：响应恰在截止步满足、
迟一步无效且不可修复、两个重叠保持窗口在**同一步**同时断裂、两种 consume 策略、
开放轨迹任何步都不得报 satisfied、相关键要求、版本/内容哈希隔离、断点恢复一致。

### 失败类别断言

`tests/error_classes.rs` 与 `tests/api.rs` 不仅检查"接口能调用"，还断言具体
`ErrorKind` 与稳定 `reason`：输入错误、状态冲突、资源耗尽、计算失败四类在
Rust API 与 HTTP 状态码两个层面都可区分。

### 重放日志

每次 `cargo test`，每个测试二进制都会在 `test_artifacts/` 下重建自己的
JSONL 重放日志 `run-log-<test-target>.jsonl`（`cargo test` 并行运行多个测试
进程，按可执行文件名分文件可以避免它们互相截断）。每行一条 JSON，包含：

- `run_id`（与 API 请求/响应的 `x-run-id` 一致）、测试名、阶段；
- `step`、该步 `spawned/resolved/violated` 与剩余 `pending` 义务 id；
- 当前 `verdict` 与人类可读的判断理由 `detail`。

拿到一个失败 run_id 后，可用对应夹具 + 日志中的中间状态逐步重放。
`test_artifacts/smoke/` 保留了一次真实 HTTP 运行（创建、逐步、切口证据、
校验通过）的完整请求/响应留档：

```
01-create.json  02-steps.json  03-final.json  04-evidence-cut2.json  05-verify.json
```

查看日志示例：

```bash
wc -l test_artifacts/run-log-*.jsonl
cat test_artifacts/run-log-*.jsonl | python3 -c "
import json,sys
for l in sys.stdin:
    e=json.loads(l)
    print(e['run_id'], e['phase'], e.get('verdict'), e['detail'])" | head
```

---

## 4. 输入语言速例

```json
{
  "id": "shop",
  "version": "1.0.0",
  "rules": [
    {
      "type": "response",
      "id": "ship_order",
      "trigger": { "event_type": "order_placed",
                   "where": { "op": "gte", "path": "priority", "value": 5 } },
      "response": { "event_type": "shipped" },
      "within_steps": 3,
      "consume": "all_matching",
      "correlation_key": "order_id"
    },
    {
      "type": "sustain",
      "id": "cool_after_fan",
      "condition": { "op": "or", "any": [
        { "op": "eq", "path": "type", "value": "fan_on" },
        { "op": "eq", "path": "cool", "value": true } ] },
      "duration_steps": 3,
      "scope": { "mode": "after_trigger",
                 "trigger": { "event_type": "fan_on" } }
    }
  ]
}
```

谓词算子：`eq/ne/gt/gte/lt/lte/in/exists/bool/not/and/or`；事实路径支持点号
（`order.id`），另有伪路径 `type`（事件类型）与 `index`（步序号）。
步：`{"index":0,"event":{"type":"order_placed","facts":{...}},"end":false}`；
裸封闭步：`{"index":N,"end":true}`。
