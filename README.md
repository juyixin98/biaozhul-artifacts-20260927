# 本地工作负载滚动发布控制器

一个完全本地运行的滚动发布（RollingUpdate）控制器：工作实例由**模拟进程管理器**
（落盘的合成夹具）提供，状态存于 **SQLite**，对外是 **Go 标准库 HTTP** 接口。
无任何生产账号或真实业务依赖。

它要回答的具体问题：

- 用**新版本启动失败**的夹具时，发布是否以确定的失败类别终止，旧版本是否保持可用；
- **就绪抖动**（探针周期性失败）时，是否要求“持续就绪”才计入可用，且最终能收敛；
- **控制器重启**后，能否重新挂载既有进程、不重复创建、继续推进到正确版本；
- **容量不足**夹具下，是否把“容量拒绝”与“实例启动失败”区分开，并守住副本约束；
- 每一步是否满足 `maxSurge` / `maxUnavailable`，最终版本是否正确；
- **创建成功是否被错误当成就绪**（本实现明确不会）；
- **回退是否是一次新操作**并保留完整发布历史。

## 模块划分（多模块后端）

```
cmd/server/                     可运行服务入口（HTTP + 后台协调循环）
internal/model/                资源模型：App/Revision/Rollout/Instance/Event/Snapshot
                                及“只有 ready 才可用”的纯函数快照计算
internal/store/                SQLite 持久化（迁移、事务、全部仓储 SQL）
internal/procman/              模拟进程管理器（Start/Status/Stop 落盘 JSON，夹具行为）
internal/controller/           协调循环（核心机制：观测→就绪判定→扩/缩→终态化）
internal/adapters/procmanager/ 适配器：procman → controller.ProcessManager 端口
internal/service/              资源编排：建应用、发起发布、回退（回退是新操作）
internal/httpserver/           标准库 HTTP 适配器、请求身份、错误语义、后台驱动
internal/config/               JSON 配置
internal/faulttests/           故障注入测试（真实 SQLite + 模拟器，直接驱动协调循环）
internal/model/*_test.go       模型不变量独立测试（期望值在测试内自行列出）
tests/blackbox/                【独立 Go 模块】仅经 HTTP/JSON 与真实服务二进制交互，
                                不 import 任何被测包，期望结果由测试自行计算
configs/                       演示与故障场景配置
scripts/run_demo.sh            本地演示（6 个场景）
scripts/run_tests.sh           执行全部测试并报告结果
```

核心机制（扩缩容决策、就绪阈值、失败分类、回退）全部在 `internal/controller`
中由代码实现，不存在硬编码演示路径；黑盒测试在另一个 go.mod 里，无法引用被测
实现，参考答案不由被测核心生成。

## 快速开始

```bash
go version          # 需要 Go 1.23+（使用纯 Go 的 modernc.org/sqlite，无需 cgo）
bash scripts/run_tests.sh
bash scripts/run_demo.sh happy
```

手动启动：

```bash
go run ./cmd/server --config configs/demo.json
# 另一终端
curl -s -X POST localhost:18080/api/apps -H 'Content-Type: application/json' \
  -d '{"name":"demo","version":"v1","replicas":3}'
curl -s localhost:18080/api/apps/demo | python3 -m json.tool
curl -s -X POST localhost:18080/api/apps/demo/deployments \
  -H 'Content-Type: application/json' -d '{"version":"v2"}'
curl -s localhost:18080/api/apps/demo/rollouts
```

数据落在 `data_dir`：`controller.db`（SQLite/WAL）与 `procman.json`（模拟进程）。

### 演示场景

`bash scripts/run_demo.sh <场景>`，会打印逐步快照（活跃/可用、新版本/旧版本、
请求 id）与最终版本、失败类别：

| 场景 | 夹具 | 期望结论 |
|---|---|---|
| `happy` | 新版本始终正常 | succeeded，最终全量 v2，先扩新就绪后缩旧 |
| `start-failure` | v2 `fail_start` | failed，类别 `start_failure`，v1 全部仍可用 |
| `jitter` | v2 每 3 次探针抖动 1 次 | 中途出现 `ready_demoted`，最终仍 succeeded |
| `capacity` | 容量池=3 且已被 v1 占满 | failed，类别 `insufficient_capacity`，无 remove_old |
| `rollback` | v2 启动失败后回退 | 回退为**新 rollout**，历史 3 条全保留，最终 v1 |
| `restart` | 发布中途杀进程再以同一数据目录启动 | 进程不重不丢，继续推进并最终 v2 |

## 协调循环语义（机制，不是演示）

每个协调滴答（tick）在**一个 SQLite 事务**内完成：

1. **观测（observe）**：对每个活跃实例调用模拟器 `Status`：
   - 探针成功则 `ready_streak++`，连续成功数 `>= ready_threshold` 才置 `ready`；
   - 探针失败则计数清零；曾就绪的实例产生 `ready_demoted` 并立即移出可用；
   - 启动后崩溃 / 宿主丢失（`Missing`）属确定失败，置 `failed` 且**永不计入可用**；
     目标版本失败实例立即终止并释放副本槽，旧版本失败实例不消耗发布失败预算、
     由缩容步骤优先回收；
   - 未知探针错误视为“不确定”，不改判失败，继续等待（见下节错误分类）。
2. **动作（act，至多一个有效伸缩动作）**，顺序可解释：
   - 先在 `maxSurge` 内容**创建新版本**（`start_new`，阶段 `starting`，**不等于就绪**）；
   - 新版本就绪后才**缩容旧版本**（`remove_old`），回收顺序：failed→starting→ready；
     回收 ready 实例后可用数仍须 `>= D - maxUnavailable`；
   - 容量池拒绝创建时，若能合规回收一个旧实例则本拍回收，下一拍重试；
   - `maxSurge=0` 且 `maxUnavailable=0` 导致零推进时判 `invalid_strategy`。
3. **终态**：旧版本全部退场且新版本可用数 `>= D` → succeeded；
   失败预算耗尽 / 容量持续不足 / 超过进度截止 → failed（带类别与原因）。

逐步的副本约束通过每个事件携带的**操作后快照**记录（见
`GET /api/rollouts/{id}/events`），字段包括：

```
total_active <= desired + max_surge          （最大额外副本，任何事件都满足）
remove_old 之后 available >= desired - max_unavailable
start_new 不改变 available                    （创建 != 就绪）
become_ready 恰好使 available +1
failed/starting 永不出现在 available 中
```

### 就绪 ≠ 创建成功

`Start` 成功只产生一条 `phase=starting, ready_streak=0` 的实例记录；只有连续
`ready_threshold` 次探针成功，才置 `ready` 并计入 `available`。事件
`start_new` 的快照中可用数不变，`become_ready` 的快照中可用数恰好 +1，
测试对这两点做了逐步断言。

## 失败类别与错误语义（区分“确定失败”与“不确定结论”）

发布终态 `status=failed` 时，`failure_category` 单列：

| 类别 | 含义 | 确定性 |
|---|---|---|
| `start_failure` | 新版本启动报错或启动后崩溃，达到失败预算 | 确定的实例级失败 |
| `insufficient_capacity` | 容量池持续拒绝创建，且无法在最小可用线内容忍旧实例退场 | 确定的环境约束 |
| `readiness_timeout` | 超过 `progress_ticks` 仍未就绪（也许稍后会好） | **不确定结论**，日志用 WARN |
| `invalid_strategy` | `maxSurge=0` 且 `maxUnavailable=0`，零推进死锁 | 确定的配置错误 |

`failure_reason` 给出人类可读原因（如 `start returned error: ...`、
`capacity pool rejects creates ...`）。模拟器在宿主重启丢失进程时，控制器
记录 `reattach` 事件并在说明里标明该实例属于新版本还是旧版本。

HTTP 状态码：

| 码 | `error` 字段 | 场景 |
|---|---|---|
| 400 | `bad_request` | JSON/字段非法（名称、版本、副本数等） |
| 404 | `not_found` | 应用/发布不存在 |
| 409 | `conflict` / `no_in_flight` | 已有在途发布时再次发布；对已终态应用手动 reconcile |
| 500 | `internal_error` | 存储等内部错误 |

每个响应回带 `X-Request-Id`（请求未提供则自动生成）；同一 id 写入发布事件
（事件的 `request_id`），协调循环自身触发记为 `tick:<rolloutID>`。因此一条
失败可以从 HTTP 响应头 → 事件流 → 服务日志三者关联，日志均带
`request_id`、`rollout`、`tick`、版本或实例位置。

## 回退是新操作，历史保留

`POST /api/apps/{name}/rollback` 找到最近成功的版本，但会**新建** revision
（`source=rollback:<failedRolloutID>`）与**新建** rollout（`op=rollback`）；
现存同版本实例直接划归新 revision（相同镜像无需重启）。失败的那次 rollout
与所有 revision 都不删除：`GET /api/apps/{name}/rollouts` 能看到
`create(succeeded) → rollout(failed) → rollback(succeeded)` 的完整序列。

## 模拟器夹具（本地合成依赖）

在配置 `fixture.behaviors[version]` 中按版本选择：

| behavior | 参数 | 行为 |
|---|---|---|
| `always_ok` | — | 启动成功，探针恒就绪 |
| `fail_start` | — | `Start` 直接返回 `start_error` |
| `crash_after` | `parameter=N` | 启动成功，N 次探针后崩溃（`crashed`） |
| `flaky` | `parameter=N` | 每 N 次探针失败一次（就绪抖动） |
| `flaky_first` | `parameter=N` | 前 N 次探针失败，之后稳定 |

`fixture.capacity>0` 时，同时存活进程达到上限即拒绝新建（返回容量错误，
与启动失败明确区分）。调试端点（仅本地）：
`POST /api/debug/procman/reset`、`.../wipe-host`（模拟宿主重启丢进程）、
`GET /api/debug/procman/list`。

## 测试如何回答这些问题

```bash
go test -race -count=1 ./...          # 主模块：模型不变量 + 故障注入
cd tests/blackbox && go test -v ./... # 独立模块：真实进程 + HTTP 黑盒
# 或一键：
bash scripts/run_tests.sh
```

- **故障注入测试**（`internal/faulttests`）：真实 SQLite + 模拟进程管理器，
  直接逐滴答驱动 `controller.Tick`。对四类夹具逐一断言**具体终态与失败类别**
  （不是“接口能调用”），并用测试自己的计数器核对实例/版本数，逐事件走查
  容量上界、`remove_old` 最小可用线、`start_new` 不增可用、`become_ready` 恰好 +1。
  `restart` 用例通过重新打开底层状态文件模拟控制器重启，并核对进程不重不丢。
- **独立黑盒测试**（`tests/blackbox`，独立 go.mod）：每个用例先 `go build`
  出真实二进制再启动，只通过 HTTP/JSON 交互；断言最终版本、实例相位、
  失败类别、逐步事件时间线、请求身份关联、以及 400/404/409 错误语义。
  它无法 import 被测包，因此不存在“核心给自己判卷”。

## 配置项

见 `configs/demo.json`：`http_addr`、`data_dir`、`tick_interval`，
默认滚动参数 `default_max_surge / default_max_unavailable /
default_ready_threshold / default_failure_limit / default_progress_ticks`，
以及 `fixture`。单次发布也可在请求体里覆盖这些参数。
`auto_rollback_on_failure=true` 时，发布失败会自动以回退形式发起一次新发布。

## 依赖

- Go 1.23+，仅标准库 HTTP；
- `modernc.org/sqlite`（纯 Go SQLite 驱动，免 cgo）；
- 演示/测试脚本需要 `curl` 与 `python3`（仅用于 JSON 展示）。
