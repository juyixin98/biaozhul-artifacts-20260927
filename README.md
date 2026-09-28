# 本地自定义资源控制器（期望态 / 实际资源服务分离）

一个纯后端的、Kubernetes 风格的自定义资源控制器演示，全部依赖均为本地合成：

- **控制器**：标准库 `net/http` 提供期望态 CRUD，SQLite 持久化，后台协调循环把期望态推向实际资源服务。
- **实际资源服务**（`fakecloud`）：独立进程、独立数据模型、内存存储，支持幂等键与乐观并发，并通过管理接口**注入故障**（创建响应丢失、创建失败、删除失败、陈旧读、读失败、延迟）。
- 不需要任何真实云账号或业务数据。

## 目录结构

```
cmd/
  controller/main.go      控制器进程：API + 协调循环 + SQLite
  fakeservice/main.go     实际资源服务进程（可故障注入）
internal/
  model/                  资源模型；规格指纹（与服务端各自独立实现）
  store/                  SQLite 存储：事务、resource_version CAS、世代单调
  reconcile/              协调循环、去重工作队列、ExternalClient 端口
  adapter/                ExternalClient 的 HTTP 适配器（错误归类，不做盲目重试）
  api/                    期望态 HTTP API（If-Match CAS、删除终结标记语义）
  fakecloud/              独立的实际资源服务与故障控制面
  diag/                   结构化 JSON 日志、请求 ID、脱敏
  config/                 环境变量配置
test/                     黑盒端到端测试（只走 HTTP，以实际服务状态为独立 oracle）
configs/config.env        配置示例
scripts/                  dev.sh（一键起两个进程）、examples.sh（示例调用）
```

模块之间是有真实职责的依赖关系：`api → store`；`reconcile → store + ExternalClient(端口)`；
`adapter` 实现该端口并对接 `fakecloud`。测试目录是外部包（`package e2e`），
不导入协调循环内部，只通过 HTTP 观察。

## 核心语义

### 三个“世代/版本”

| 字段 | 含义 | 谁来推进 |
|---|---|---|
| `metadata.generation` | 期望世代：用户每改一次 spec +1 | API |
| `status.observedGeneration` | 控制器已**观察并做出决策**的世代 | 协调循环 |
| `status.reconciledGeneration` | 已被实际服务**确认承载**该规格的世代 | 协调循环，仅在确认后 |

- 观测失败、结果不明、陈旧读：**不会**推进 `reconciledGeneration`。
- 存储层对世代字段做 `max()` 合并，迟到的旧观测不可能让进度倒退。
- `metadata.resourceVersion` 是本地行版本：API 写入与状态写入都以它做 CAS。

### 删除：终结标记（finalizer）分阶段

1. 资源创建外部对象前先加 `resourcecontroller.dev/external-cleanup` finalizer。
2. `DELETE` 只设置 `deletionTimestamp`（API 返回 202）。
3. 协调循环删除外部对象；删除失败则带指数退避重试，**记录、finalizer、外部对象都保留**。
4. 观察到外部 404 → 移除 finalizer；再一次观察确认 404 → 才删除本地记录。
5. 若 finalizer 已移除但外部对象重新出现，会重新加回 finalizer，拒绝删记录。

### 创建中断：先查询认领，绝不重复创建

外部对象 ID 由资源名确定性派生（`w-<sha256>`），创建带按世代派生的幂等键。
创建返回 5xx/超时时结果归为 **ambiguous**：控制器记录“无法判定”，下一轮直接 GET 同一 ID：
对象在则认领，404 才创建。因此“外部创建成功但响应丢失”只会产生一个对象。

### 冲突更新：重排队而不是覆盖新规格

- 本地 `resource_version` 冲突 → 重新入队、重新加载，API 侧返回 409。
- 外部对象版本冲突（`If-Match` 过期）→ 记为 rejected/`external-version-conflict`，
  重新加载实际状态后再决策，绝不盲写覆盖更新的实际规格。

### 陈旧观测

外部更新成功后的确认读若返回更新前快照，两次读取不一致时判为
`stale-observation` / `post-update-mismatch`（undecidable），世代不完成，下轮重读确认。

## 快速开始

要求 Go 1.23+（唯一三方依赖是纯 Go 的 `modernc.org/sqlite`，无需 CGO）。

```bash
go build ./...
make test          # 全部测试（含黑盒 e2e）
make test-race     # 竞态检测

make dev           # 构建并用示例配置同时启动两个进程
# 另一个终端：
./scripts/examples.sh
```

或手动启动：

```bash
go build -o bin/fakeservice ./cmd/fakeservice
go build -o bin/controller  ./cmd/controller
./bin/fakeservice                              # 监听 127.0.0.1:8090
RC_HTTP_ADDR=127.0.0.1:8080 ./bin/controller    # 监听 127.0.0.1:8080
```

配置见 `configs/config.env`：`RC_HTTP_ADDR`、`RC_DB_DSN`、`RC_EXTERNAL_URL`、
`RC_WORKERS`、`RC_RESYNC_INTERVAL`、`RC_BACKOFF_BASE/MAX`、`FAKE_HTTP_ADDR`。

## HTTP 接口

### 控制器（期望态）

| 方法/路径 | 说明 |
|---|---|
| `POST /api/v1/widgets` | 创建（body 含 `metadata.name` 与 `spec`） |
| `GET  /api/v1/widgets` / `/{name}` | 列表 / 获取（**不回显密钥**，仅回脱敏值） |
| `PUT  /api/v1/widgets/{name}` | 更新 spec，必须带 `If-Match: <resourceVersion>`；成功则 generation+1 |
| `DELETE /api/v1/widgets/{name}` | 设置删除时间戳（幂等，202） |

创建示例：

```bash
curl -s -X POST localhost:8080/api/v1/widgets \
  -H 'Content-Type: application/json' \
  -d '{"metadata":{"name":"demo"},
       "spec":{"replicas":3,"color":"blue","secretToken":"demo-secret-1"}}'
```

响应的 `status` 给出三个世代、外部 ID/版本、阶段、条件和最近一次决策
（`lastAttempt`：阶段 / accepted|rejected|undecidable / 原因 / 失败类别 / 请求 ID）。

### 实际资源服务（fakecloud）

| 方法/路径 | 说明 |
|---|---|
| `PUT /v1/widgets/{id}` | 幂等创建/更新；创建读 `Idempotency-Key`，更新读 `If-Match` |
| `GET /v1/widgets/{id}`、`DELETE`、`GET /v1/widgets` | 对象操作（删除不存在对象视为成功） |
| `PUT /internal/faults/{op}` | 注入故障：`{"kind":..., "times":N}`，op ∈ create/update/get/delete |
| `DELETE /internal/faults[/{op}]`、`GET /internal/status` | 清除/查看故障 |

故障类型：`createResponseLost`（已提交但返回 500）、`createFail`、`deleteFail`、
`staleGet`（更新后返回一次旧快照）、`getFail`、`slow`。

```bash
# 制造“创建成功但响应丢失”
curl -s -X PUT localhost:8090/internal/faults/create \
  -H 'Content-Type: application/json' -d '{"kind":"createResponseLost","times":1}'
# 制造一次删除失败
curl -s -X PUT localhost:8090/internal/faults/delete \
  -H 'Content-Type: application/json' -d '{"kind":"deleteFail","times":1}'
```

## 诊断与脱敏

所有日志为单行 JSON。协调决策统一为 `"msg":"reconcile decision"`，带：

- `requestId`（入站 `X-Request-Id` 透传，未提供则生成，并随外部调用转发）
- `resource`、`generation`、`observedGen`、`reconciledGen`、`externalId`、`extVersion`
- `phase`（create/update/delete/observe）、`decision`（accepted/rejected/undecidable）
- `action`、`reason`、`category`（失败类别）、`detail`

示例（真实运行截取）：

```json
{"msg":"reconcile decision","resource":"lost","phase":"create",
 "decision":"undecidable","action":"claim-by-observe-next-pass",
 "reason":"ambiguous","category":"HTTP500","generation":1,
 "observedGen":1,"reconciledGen":0,"requestId":"3a55a151…"}
{"msg":"reconcile decision","resource":"demo","phase":"delete",
 "decision":"accepted","action":"remove-record",
 "reason":"external-cleanup-confirmed", …}
```

`secretToken` 在两处都以 `diag.Secret`/`Redact` 输出（形如 `hu****t`）；
API 响应不返回该字段。测试会断言完整密钥不出现在任何日志或响应中。

## 测试

```bash
go test ./... -count=1 -race
```

- `internal/store`：CAS 冲突、世代单调、finalizer 幂等与删除、待处理列表。
- `internal/reconcile`：脚本化的独立内存 `ExternalClient`，逐轮驱动协调，
  断言具体结果与失败类别——重复事件去重、创建中断只认领不重建、
  陈旧读不完成世代、外部版本冲突拒绝/重载/再应用、删除失败重试与各阶段归属、
  创建失败重试。
- `test/`（黑盒 e2e）：真实 HTTP 启动控制器 + 实际服务 + 真实协调循环与 SQLite，
  仅通过 HTTP 交互；“实际归属”的断言取自 **fakecloud 自己的 API 和它自己的日志**，
  不由被测控制器生成。覆盖：重复事件只产生一个对象且同规格不重写、
  创建响应丢失后恰好一个对象、删除重试期间三方归属保持、陈旧读不推进世代、
  API 409 CAS、请求 ID 关联与密钥零泄露。

### 已验证的真实运行结果

两个二进制实际启动后注入故障验证：响应丢失 → 控制器记录
`undecidable/ambiguous → claim-by-observe → in-sync`，实际服务对象数保持 2；
删除失败 → `undecidable retry-delete`，持续故障下记录停留在 `Deleting`、
finalizer=1、外部对象 GET 200；故障解除后记录与外部对象都变为 404；
完整密钥在两个进程日志中出现 0 次，仅出现 `hu****t` / `to****e`。

## 设计取舍与剩余限制

- **单节点**：工作循环只在一个控制器进程内运行；没有选主。SQLite 用单连接串行化写，
  多进程写同一个库不在支持范围内（需换 Postgres + 锁才能水平扩展）。
- **确定性外部 ID**：对象 ID 由资源名派生，简化了“断后认领”；代价是不支持改名，
  改名应建模为新建+删除。
- **陈旧读防御是双读确认**：在故障模型（单次旧快照）下足够；对“持续返回一致旧数据”
  的拜占庭副本无法仅靠多读识别，需要服务端提供版本/时间戳保证（本模型已有版本号，
  生产环境应额外要求单调读）。
- **规格指纹包含密钥**：指纹用于比较实际/期望规格，密钥轮换会产生一次更新，这是有意的。
- **退避与抖动**：退避为指数封顶；当前未加随机抖动，多资源同步故障时会形成较整齐的重试节奏。
- **无鉴权/TLS**：仅监听回环地址、用于本地演示；`/internal/faults` 绝不能暴露到非信任网络。
- **故障注入面**：fakecloud 重启即丢状态（内存存储）；这符合“测试夹具”定位。
