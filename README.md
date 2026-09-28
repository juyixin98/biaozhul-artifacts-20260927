# rollctl — 本地工作负载滚动发布控制器

一个完全本地化、可复现的滚动发布（rolling update）控制器。工作实例由一个**模拟进程管理器**
提供（合成故障夹具），所有状态保存在单个 **SQLite** 文件中，对外用 **Go 标准库 HTTP** 暴露 API。
不需要任何生产账号或外部服务。

它回答四个具体问题，且每个结论都有逐步断言：

1. **新实例启动失败**时发布如何收场、失败归为哪一类、旧版本是否继续服务；
2. **就绪抖动**（readiness jitter）时"创建成功"为何不等于"就绪"，发布为何不能切流；
3. **控制器/进程重启**后发布能否从同一 SQLite 状态恢复并收敛；
4. **进程管理器容量不足**时控制器如何在不破坏副本约束的前提下阻塞，并给出精确失败类别。

---

## 架构（多模块后端，各司其职）

```
cmd/rollctl/           可运行服务入口（HTTP + 可选自动协调循环）
internal/model/        资源模型：Workload / Revision / Instance / Release / Event
internal/store/        SQLite 持久层（资源表 + 发布历史 + 审计事件 + 模拟器状态）
internal/controller/   协调循环：唯一承载发布策略的核心
internal/adapter/      参与者适配器：ProcessManager 接口 + 合成故障夹具 Simulator
internal/api/          标准库 net/http API（请求关联 ID、结构化错误）
internal/config/       flag/env 配置
acceptance/            独立黑盒验收测试（仅通过 HTTP；含真实二进制重启夹具 + 独立参考预言机）
scripts/demo.sh        本地演示脚本（curl 驱动，逐场景）
```

关键边界：

- **策略只存在于 `internal/controller`**。持久层不判断 surge/unavailable，适配器不判断就绪阈值，
  任何演示路径都不能硬编码结果。
- **独立测试不导入被测核心**。`acceptance/` 只发 HTTP 请求，期望由本包内独立编写的参考预言机
  （oracle）从"基线副本数 + 声明的策略"重新推导，因此参考结论不可能由核心实现自己生成。
- 故障夹具（`internal/adapter`）是一个独立模块，它决定进程何时起、何时就绪、何时抖动/退出/拒绝，
  并单独有测试。

---

## 核心机制

### 每一步都满足的副本约束

对基线副本数 `N`、策略 `maxSurge=S`、`maxUnavailable=U`，在协调循环的**每个 tick** 之后：

- `live ≤ N + S`（存活进程数，含尚未就绪的新实例）；
- `available ≥ N - U`（持续就绪、真正可服务的实例数）。

测试在发布处于 pending/active 的**每一个 tick** 后都用独立 oracle 检查这两条，而不是只看终态。

### "创建成功"不是就绪

- 进程管理器 `Start` 返回句柄只表示进程被创建，实例状态为 `starting`，**不计入 available**。
- 就绪需要连续 `readyThresholdTicks` 次 ready 观测；中间出现一次 not-ready，连续计数清零。
- 已经 ready 的实例若之后丢失就绪（`ready_lost` 夹具），立即降级回 `starting` 并从可用数剔除。
- `failed` / `terminated` 实例永远不计入 live 或 available（保留行作为历史墓碑）。

### 可解释的扩缩容顺序

每个 tick 至多做**一个**结构性动作，顺序固定：

1. 在 surge 预算内启动一个新版本实例（只创建，不代表可用）；
2. 新实例经过阈值观测逐个变 ready；
3. 只有在 `available > N-U` 不变量成立时，才移除一个旧版本实例，为下一个新版本实例腾位；
4. 新版本可用数达到 `N` 且无旧版本存活实例时，发布才成功并切换 `currentRevision`。

一旦新版本出现就绪不稳定（抖动），旧版本缩容会被**冻结**，直到新版本全部重新稳定就绪；
这样不会用"已证明可靠的旧容量"去换"尚未证明可靠的新副本"。

### 失败语义（闭合类别）

| 类别 `failureCategory` | 触发事实 | 发布结果 |
|---|---|---|
| `start_failed` | 新实例被管理器拒绝启动，或启动后、就绪前进程退出，且超过 `maxStartFailures` 预算 | failed；旧版本继续完整服务，`currentRevision` 不变 |
| `readiness_flapping` | 没有硬启动错误，但就绪反复抖动/就绪后丢失，在 `deadlineTicks` 内无结构性进展 | failed；旧版本继续服务 |
| `insufficient_capacity` | surge 允许的新启动被进程管理器以容量满拒绝，且移除旧实例会击穿可用下限 | failed；全程不破坏约束 |
| `rollout_stalled` | 策略本身不可行（如 surge=0 且 unavailable=0）或其他无进展停滞 | failed；明确区别于容量故障 |

事件流区分**不确定结论**与**硬失败**：

- 容量暂时打满、一次就绪抖动 → `certain=false` 的 `warn` 事件（可能在后续 tick 自行恢复）；
- 发布最终失败 → `certain=true` 的 `fail` 事件，带明确 `category`。

### 回退是一次全新操作

`POST /workloads/{name}/rollback` 总是创建一条**新的** release 行（`kind=rollback`，记录
`rollbackOf`），走同一台滚动状态机；旧的成功/失败 release 行全部保留。不给目标版本时，自动选择
最近一个不同于当前版本的**曾成功**版本。

### 控制器重启

逻辑时钟（tick）、release、instance、事件以及**模拟进程表**都在 SQLite 中。重启后：

- tick 从持久值继续（不重置）；
- 模拟器从进程表重挂，就绪计时基于进程 `startedTick` 继续，不会因重启丢失；
- 进行中的 release 仍是同一条 active 行（同一 id、同一 request id），继续推进。

---

## 运行

要求：Go 1.23+（SQLite 使用纯 Go 驱动 `modernc.org/sqlite`，无需 C 工具链）。

```bash
make build           # 构建 bin/rollctl
make test            # 运行全部测试（含故障夹具、独立黑盒、真实二进制重启）
make test-race       # 加 -race
make demo            # 构建并运行全部本地演示场景
```

手动 tick 模式启动服务（时间只在调用 `/admin/tick` 时前进，便于复现）：

```bash
./bin/rollctl -manual -http 127.0.0.1:8080 -db ./data/demo.db -sim-capacity 16
```

自动协调循环模式（默认每 500ms 一个 tick）：

```bash
make run             # 或 ./bin/rollctl -db ./data/demo.db
```

标志 / 环境变量：`-http`/`ROLLCTL_HTTP`、`-db`/`ROLLCTL_DB`、`-tick`/`ROLLCTL_TICK`、
`-manual`/`ROLLCTL_MANUAL=1`、`-sim-capacity`/`ROLLCTL_SIM_CAPACITY`。

### 复现步骤（以启动失败夹具为例）

```bash
./bin/rollctl -manual -db /tmp/r.db &

# 1) v1 健康；bad 版本启动即被拒绝
curl -s -X PUT localhost:8080/admin/simulator/workloads/w/revisions/v1/behavior \
  -d '{"mode":"normal"}'
curl -s -X PUT localhost:8080/admin/simulator/workloads/w/revisions/bad/behavior \
  -d '{"mode":"start_rejected"}'

# 2) 创建 2 副本工作负载并 tick 到稳态
curl -s -X POST localhost:8080/api/v1/workloads -H 'X-Request-Id: seed' -d '{
  "name":"w","replicas":2,"revision":"v1",
  "policy":{"maxSurge":1,"maxUnavailable":0,"readyThresholdTicks":2,"deadlineTicks":20}}'
for i in $(seq 1 20); do curl -s -X POST localhost:8080/admin/tick >/dev/null; done

# 3) 发布到 bad，带关联请求 ID
RID=$(curl -s -X POST localhost:8080/api/v1/workloads/w/releases \
  -H 'X-Request-Id: repro-77' -d '{"revision":"bad"}' | grep -o '"id":"[^"]*"' | cut -d'"' -f4)
for i in $(seq 1 20); do curl -s -X POST localhost:8080/admin/tick >/dev/null; done

# 4) 检查精确失败类别、最终版本、以及按请求 ID 关联的事件链
curl -s localhost:8080/api/v1/releases/$RID
#   state=failed, failureCategory=start_failed
curl -s localhost:8080/api/v1/workloads/w | grep -o '"currentRevision":"[^"]*"'   # 仍是 v1
curl -s "localhost:8080/api/v1/workloads/w/events" | grep repro-77
```

其他夹具：`crash_after_start`（启动后退出）、`flap`（就绪抖动）、`ready_lost`（就绪后丢失）、
容量用 `PUT /admin/simulator/capacity {"capacity":N}` 调小。或直接：

```bash
./scripts/demo.sh happy       # 健康滚动
./scripts/demo.sh startfail   # 启动失败 -> start_failed
./scripts/demo.sh flap        # 就绪抖动 -> readiness_flapping
./scripts/demo.sh capacity    # 容量不足 -> insufficient_capacity
./scripts/demo.sh restart     # 发布中途杀掉进程并用同一 DB 重启
./scripts/demo.sh all         # 全部（每个场景独立 DB）
```

---

## HTTP 接口与错误语义

每个响应都带 `X-Request-Id`（请求未提供则生成）。变更类请求把该 id 记录在 release 行与事件链上，
失败可从一次请求端到端追到关键步骤、版本、tick 和失败类别。

| 方法与路径 | 说明 |
|---|---|
| `GET /healthz` | 存活检查 + 当前持久 tick |
| `POST /api/v1/workloads` | 创建工作负载（同时写 bootstrap release） |
| `GET /api/v1/workloads` | 列出工作负载 |
| `GET /api/v1/workloads/{name}` | 状态：基线/live/available、按版本计数、实例列表 |
| `GET /api/v1/workloads/{name}/instances` | 实例（含 failed/terminated 墓碑） |
| `GET /api/v1/workloads/{name}/events?afterSeq=` | 审计事件（可增量轮询） |
| `POST /api/v1/workloads/{name}/releases` | 创建滚动发布（pending） |
| `GET /api/v1/workloads/{name}/releases` | 完整发布历史（最新在前） |
| `GET /api/v1/releases/{id}` | 单个发布 |
| `POST /api/v1/workloads/{name}/rollback` | 回退（全新 rollback release；body 可给 `targetRevision`） |
| `POST /admin/tick` | 推进一个逻辑 tick 并对所有工作负载协调一次 |
| `GET|PUT /admin/simulator/capacity` | 读取/设置模拟管理器槽位 |
| `PUT /admin/simulator/workloads/{w}/revisions/{r}/behavior` | 安装故障夹具 |

错误响应统一为：

```json
{ "error": "active_release", "category": "active_release",
  "requestId": "req-...", "message": "controller: a release is already pending or active: release r-..." }
```

| HTTP | category | 含义 |
|---|---|---|
| 400 | `bad_request` | 参数/JSON 非法（未知字段也拒绝） |
| 404 | `not_found` | 工作负载或 release 不存在 |
| 409 | `active_release` | 已有 pending/active 发布，拒绝并发发布 |
| 422 | `no_rollback_target` | 目标版本从未在该工作负载上成功过 / 无可回退版本 |
| 500 | `internal_error` / `tick_failed` | 服务端内部错误 |
| 503 | `simulator_unavailable` | 当前装配未暴露模拟器管理接口 |

发布自身的成败不走 HTTP 错误码（创建发布成功是 201）；失败结论在 release 的
`state=failed` 与 `failureCategory` 中，旧版本继续服务。

---

## 测试如何回答四个问题

- `internal/controller/fixtures_test.go`（故障模块的直接夹具测试）：
  逐步断言 surge/unavailable；断言新建实例 `starting/streak=0`、不可用；断言旧实例在任何新实例
  ready 之前不被移除；四类夹具的精确终态与类别；重启中途恢复；回退历史与请求关联。
- `internal/adapter/sim_test.go`：独立验证参与者语义（未就绪、拒绝、崩溃、抖动交替、容量、重挂）。
- `acceptance/acceptance_test.go`：纯 HTTP 黑盒，oracle 每 tick 独立校验；断言线上具体类别、
  最终版本、计数、事件的 certain 标志、404/409/422/400 错误语义。
- `acceptance/restart_test.go`：`go build` 真实二进制 → 起进程 → 发布中途 `kill` →
  以同一 SQLite 文件重新起进程 → 断言 tick 单调继续、同一 release 恢复并收敛、历史与请求关联保留。

```bash
go test -count=1 ./...          # 全部通过才算成功
go test -race -count=1 ./...    # 竞态检测
```

## 依赖清单

见 `go.mod`：运行期仅 `modernc.org/sqlite`（纯 Go SQLite，无需 cgo）及其传递依赖；
其余全部为 Go 标准库。
