# replicactl — 本地副本数控制器

一个**完全本地、自包含**的副本数（replica）控制器：输入是按合成实例上报的负载样本，
输出是对本地“机队夹具”的扩缩容决策。技术栈为 **Go + 标准库 `net/http` + SQLite**
（纯 Go 驱动 `modernc.org/sqlite`，无需 cgo、无需任何生产账号或真实业务数据）。

它不是硬编码演示：核心是一个可注入时钟、可注入存储端口的**确定性协调循环**，并有一套
**独立参考答案实现**（`internal/oracle`，不 import 被测核心）逐 tick 手算期望副本数、
动作类别与失败类别进行验收。

---

## 1. 模块划分（多模块后端，各负其责）

| 模块 | 职责 |
|------|------|
| `internal/model` | 资源模型（机队/实例、样本、需求信号、可解释决策记录） |
| `internal/config` | 策略配置：目标负载、新鲜度、双窗口、速率上限、零副本策略；默认值、校验、版本 |
| `internal/controller` | **协调循环核心**：缺测分类、聚合、目标公式、滞后带、缩容稳定窗口、扩容速率上限、零副本、执行 |
| `internal/store` | SQLite 持久化 + **本地夹具执行器**（机队表，稳定且不复用的实例 ID） |
| `internal/adapter` | 标准库 HTTP 适配器：请求身份关联、结构化日志、错误信封、配置/摄入/协调接口 |
| `internal/app` | 装配：建库、种子机队、后台循环、HTTP server |
| `internal/oracle` | **独立参考答案**（仅测试使用，不依赖核心实现） |
| `internal/testkit` | 确定性测试夹具：内存 Port、可控时钟、自动用 oracle 打分 |
| `internal/fault` | **故障注入测试**：执行器拒绝、存储/读取失败、信号不可读 |
| `internal/accept` | **独立黑盒验收**：构建真实二进制、起真实 HTTP/SQLite、真实进程重启 |
| `cmd/replicactl` | 服务入口 |
| `cmd/acceptance` | 验收器入口（退出码可用于把关） |

---

## 2. 算法规格（行为约定的精确定义）

配置见 `configs/config.json`，关键字段：`target_load_per_instance=T=100`、
`tolerance=0.10`、`metric_freshness=60s`、`scale_down_stable_window=5m`、
`scale_up_max_factor=2.0`、`scale_up_max_delta=4`、`bootstrap_replicas=1`。

### 2.1 缺测/延迟的保守处理与分类
对每个**当前活跃实例**，取该指标的最新一条上报，按 tick 时刻 `now` 分类：

- **fresh（新鲜）**：`now - observed_at <= metric_freshness`，其值参与聚合。
- **stale（延迟/过期上报）**：存在上报但已超过新鲜度窗口。该值**被排除在聚合之外**，
  记录不确定项；**过期指标不得触发扩容**。
- **missing（缺测）**：根本没有上报。保守地按“它仍在承担目标负载”处理——
  为其**计入一份目标负载 T 的虚拟值（imputation）**，并且有一条硬性规则：
  只要存在缺测实例，**禁止缩容**。

### 2.2 指标聚合
```
measuredLoad = Σ fresh 实例上报值
imputedLoad  = (#missing) × T
totalLoad    = measuredLoad + imputedLoad
usageRatio   = totalLoad / (currentReplicas × T)
```
stale 实例既不计入 measured 也不做 imputation（它不是“沉默”，而是“上报了但不可信”）；
其存在会单独作为不确定项输出。

### 2.3 目标计算公式 + 滞后带
- 若 `|usageRatio - 1| <= tolerance`（±10%）：`desired = current`，**不动作**。
- 否则低载向下取整、高载向上取整，避免临界抖动：
  - `usageRatio > 1`：`desired = ceil(totalLoad / T)`
  - `usageRatio < 1`：`desired = floor(totalLoad / T)`
- 缺测硬守卫：缺测时若公式想缩，则 `desired = current`。
- 边界裁剪：`desired = clamp(desired, min_replicas, max_replicas)`。

### 2.4 两个**分别维护**的速率/稳定机制
- **扩容速率上限**（即时生效，单 tick 封顶）：
  `ceiling = min(ceil(current × scaleUpMaxFactor), current + scaleUpMaxDelta)`。
- **缩容稳定窗口**（Kubernetes 式 stabilization，独立维护）：
  每个进入缩容分支的 tick 记录一个“缩容建议值”。只有当
  (a) 存在一个**至少满一个窗口**的锚点读数，且
  (b) 窗口内（`0 < age <= window`）所有读数都 `<= 候选值`，
  才真正缩到候选值；否则保持当前。窗口内出现过更高的读数，则最多缩到该窗口最大值。
  → **短尖峰后的迅速回落不会立即缩容**。

### 2.5 过期指标不能触发扩容 / 零副本独立策略
- 当 `current > 0` 且没有任何 fresh 实例（全 stale 或全 missing）：**保持**，
  理由分别为 `ALL_METRICS_STALE_HOLD` / `MISSING_INSTANCE_BLOCKS_DOWNSCALE`。
- 当 `current == 0`：负载/T 在 0 处无定义，走**独立的零副本策略**，不套用除法：
  - 仅当存在**新鲜**外部需求信号（`demand.pending > 0` 且年龄 `<= demand_freshness`）
    时，引导到 `bootstrap_replicas`（默认 1）；
  - 无信号 / 信号过期 / pending=0 → 保持 0，理由 `ZERO_NO_FRESH_DEMAND`。

### 2.6 只调本地夹具
执行器 `store.ApplyScale` 只改 SQLite 中的合成机队表：扩容追加新的稳定 ID
（`ins-0001`… 全局自增、**永不复用**），缩容停用编号最大的活跃实例。全程无任何外部 API。

---

## 3. 可解释性（接口结果与日志）

每次协调都产出一条 `Decision` 审计记录（持久化于 `decisions` 表，可经
`GET /v1/decisions/{requestID}` 取回），包含：

- **请求身份关联**：`request_id`（取自/回填 `X-Request-ID`，响应头同名回显）。
- **关键步骤的数值**：`current/desired_raw/desired/applied`、`measured_load`、
  `imputed_load`、`total_load`、`usage_ratio`、fresh/stale/missing 实例清单。
- **版本/处理位置**：`config_version`、`config_revision`（仅在配置真正变化时递增，
  重启保持不变）、`location`（产生决策的代码位置）。
- **失败原因单列**：`reasons[].code/message`（稳定的机器可读类别码）。
- **不确定结论单列**：`uncertainties[]`（缺测按目标负载假设、过期上报被排除、部分覆盖）。
- **执行器失败**：`actuator_error` + `ACTUATOR_ERROR` 类别，`applied` 如实反映未变更，
  绝不假装成功。

结构化 JSON 日志（`slog`）对每次摄入与协调输出相同字段，便于按 `request_id` 串联。

---

## 4. HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `GET /healthz` | 存活探针 |
| `GET /config` / `PUT /config` | 读取 / 局部合并替换配置（校验失败返回 `INVALID_CONFIG`，并 bump revision） |
| `POST /v1/instances/{id}/samples` | 上报一个负载样本 `{metric?,value,observed_at?}` |
| `POST /v1/demand` | 上报零副本外部需求 `{pending,observed_at?}` |
| `POST /v1/reconcile` | 触发一次确定性协调，返回完整决策 |
| `GET /v1/fleet` | 当前资源模型（副本数与实例 ID） |
| `GET /v1/decisions?limit=N` | 最近决策 |
| `GET /v1/decisions/{requestID}` | 按请求身份取回某次决策 |

错误统一为 `{error,code,request_id}`，例如 `BAD_VALUE`、`BAD_JSON`、`INVALID_CONFIG`、
`NOT_FOUND`、`RECONCILE_FAILED`。

---

## 5. 快速开始

```bash
# 构建
go build ./...

# 用短窗口演示配置启动（20s 缩容窗口，便于观察；默认配置是 5m）
go run ./cmd/replicactl \
  -db file:demo.db -config configs/demo.fast.json \
  -addr 127.0.0.1:18080 -no-autotick

# 另一个终端：调用示例
bash examples/curl-examples.sh
```

服务调用示例逐步演示负载阶跃、延迟过期上报（应**不**扩容），并按请求 ID 取回决策。

---

## 6. 验收方式（可核验结果）

一键复现（构建 + vet + 单元/故障/存储/HTTP 测试 + 独立黑盒验收，并留存结果）：

```bash
bash scripts/reproduce.sh
# 结果保存于 results/unit-tests.txt 与 results/acceptance-report.txt
```

或直接运行独立验收器：

```bash
go run ./cmd/acceptance -workdir /tmp/accept
```

验收器会：
1. 用 `go build` 构建**真实服务二进制**；
2. 启动真实 HTTP + SQLite 服务，逐场景注入：**负载阶跃、缺测、延迟（过期）报告、
   短尖峰、零副本需求信号**；
3. 每个 tick 用**独立 oracle 手算**期望副本数/动作/失败类别，与真实响应逐字段比对；
4. 执行一次**真实进程重启**（同一 DB 文件），断言副本数、稳定 ID、扩容序列号（不复用）持久化；
5. 打印每次“不动作”的理由，失败时以非零退出码退出。

### 为什么参考答案是独立的
`internal/oracle` **不 import** `internal/controller`：它依据本 README 的规格，用直白、
可在纸面上复核的算术独立重算。被测核心不可能“自己给自己判卷”。`internal/testkit` 在
单元层也用同一个 oracle 对每个 tick 自动打分。

---

## 7. 测试覆盖

- `internal/controller`：阶跃扩容、速率封顶、缩容稳定窗口（首读延迟/窗口未满/满窗口缩容/
  窗口内高读数阻挡）、短尖峰不立即缩、缺测禁缩、过期不扩、滞后带、零副本（无/过期/正常）。
- `internal/fault`：执行器拒绝扩容、观察写入/读取失败、样本读取失败、审计写入失败、
  零副本需求探测失败——逐一断言**具体结果与失败类别**。
- `internal/store`：真实 SQLite 的重启持久化、实例 ID 不复用、配置 revision、修剪、
  无数据行的标准错误。
- `internal/adapter`：端到端摄入→协调→按请求 ID 取回、错误类别信封、非法配置拒绝、
  零副本引导、请求身份回显。
- `internal/accept`：上述全部 + 真实进程重启的黑盒验收。

---

## 8. 依赖锁定 / 复现

- `go.mod` / `go.sum` 已提交，固定 `modernc.org/sqlite v1.36.1` 及全部传递依赖。
- 纯 Go 驱动，**无 cgo**；Go 1.23。需要离线/空气间隙复现可执行 `go mod vendor`
  后用 `go build -mod=vendor ./...`（仓库默认不携带 vendor 目录以保持精简）。
- 所有数据均在本地临时 SQLite 文件或内存夹具中生成，结束即弃，无外部副作用。
