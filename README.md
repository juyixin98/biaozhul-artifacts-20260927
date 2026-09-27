# pathvector — 固定小型自治系统拓扑的路径向量收敛后端

一个**只接受合成 UPDATE 事件**的 BGP 风格路径向量（path-vector）收敛回放引擎。
不连接任何公网邻居、不解析线路 BGP；所有拓扑、策略与事件都来自本地 JSON
夹具（fixture）。引擎逐跳传播内部 UPDATE，直到网络静止、被证明振荡，或耗尽
步进预算。

## 1. 支持范围（行为契约）

### 1.1 支持的选路属性与比较顺序

路由器在 Adj-RIB-In 的全部候选之间按以下**固定顺序**比较，第一个决出胜负的
步骤会写入轨迹的 `reason` 字段：

| 顺序 | 属性 | 更优条件 |
|----|------|----------|
| 1 | local-pref | 越高越好 |
| 2 | AS_PATH 长度 | 越短越好（只支持一个 AS_SEQUENCE，不支持 AS_SET） |
| 3 | ORIGIN | IGP < EGP < INCOMPLETE |
| 4 | MED | 越低越好；**仅当两条路径最左（相邻）AS 相同才比较**，否则跳过该步 |
| 5 | 来源类型 | 本地起源 > eBGP 学到 > iBGP 学到 |
| 6 | 到出口的 IGP 代价 | 越低越好（early-exit） |
| 7 | 邻居 router id | 声明序越小越好（最终确定性平局裁决） |

**AS_PATH 环抑制（硬规则，先于选路与导入策略）**：收到的 UPDATE 若
AS_PATH 中已包含本路由器的 ASN，直接拒绝（轨迹 `outcome=rejected_loop`，
`reason=as_path_contains_local_as`），该候选不会进入 RIB-In，导入策略也不会
被求值。

### 1.2 撤回语义

撤回（`withdraw`）**只删除指定邻居来源的那一个候选**，以
`(router, prefix, peer)` 为键，绝不触碰其他邻居的候选。删除后重跑选路，
备选路径被提升（promotion）。撤回一个从未通告的候选是
`state_conflict` 错误，且删除数量为 0。

### 1.3 导入 / 导出策略独立

- 每个路由器分别配置 `import` 与 `export` 规则链，**首条命中生效**，无命中
  默认放行；
- 导入可以 `set_local_pref`；导出可以 `set_med` 与 `prepend`（在强制 eBGP
  自带 ASN 之外再追加本 ASN）。属性设置方向与数据面职责一致，配置期会拒绝
  反向使用（如导入链写 `prepend`）；
- 导出拒绝只影响"向该邻居"的发布，不影响本地选路，也不影响向其他邻居发布；
- eBGP 发布强制前置本 ASN 并重写下一跳；iBGP 发布保留 AS_PATH 并采用
  next-hop-self；iBGP 水平分割（iBGP 学到的路由不再转给其他 iBGP 邻居）；
  通用水平分割（不向来源邻居回发）。

### 1.4 不收敛与振荡证据

- **预算耗尽**：处理步数（种子事件 + 内部消息）达到 `budget` 仍有队列 →
  `status=not_converged, reason=budget_exceeded`（这是结论，不是错误，HTTP 200）；
- **振荡证明**：每处理一步后对全局状态签名（所有路由器所有前缀的最佳路由
  属性 + 完整 FIFO 队列序列）。签名重复即确定性未来重现，返回
  `reason=oscillation_detected` 及 `cycle` 证据（`first_step`、
  `second_step`、状态游走 `walk` 与重复的签名）。
- **队列上限**：待发内部 UPDATE 数超过 `queue_cap` → 硬错误
  `resource_exhausted`（HTTP 429）。

## 2. 工程组织

```
internal/
  ierr/      跨模块错误契约：invalid_input | state_conflict |
             resource_exhausted | computation_failed | not_found
  model/     网络模型：拓扑/节点/链路校验、路径属性、候选、事件与快照
  config/    配置解析与校验：拓扑、独立导入/导出策略、合成种子事件、限额
  engine/    收敛引擎：环抑制、策略应用、RIB-In/Out、选路比较器、
             逐跳传播、预算与循环签名、轨迹与报告（compare.go 为比较顺序）
  store/     SQLite 持久化：runs 与逐行 run_events（便于按步重放问题）
  replay/    编排边界：run id 生成、日志、调用引擎并归档
  api/       本地 HTTP 回放接口（Go 1.22+ 方法路由）
cmd/
  pvserver/  常驻本地 HTTP 服务
  pvreplay/  离线单夹具回放，报告 JSON 输出到 stdout
testdata/fixtures/  合成夹具（手工构造，含各自 description）
```

模块间只通过 `ierr.Kind`、`model.*` 值类型与 `engine.Report` 这些显式契约
通信；内部 `Message` 不从外部反序列化，外部唯一入口是种子事件 schema。

## 3. 错误类别（可区分）

| Kind | 触发示例 | HTTP |
|------|----------|------|
| `invalid_input` | JSON 非法/未知字段、拓扑悬空链路、前缀非规范、策略引用不存在节点、未知事件类型 | 400 |
| `state_conflict` | 撤回不存在的（来源,前缀）候选 | 409 |
| `resource_exhausted` | 内部消息队列超 `queue_cap`、请求体超限、存储满 | 429 |
| `not_found` | 查询不存在的 run | 404 |
| `computation_failed` | 存储 I/O 错误、内部不变量破坏 | 500 |

预算耗尽与振荡**不是错误**：它们是 `not_converged` 结论（HTTP 200），
报告带原因和证据。硬错误也会归档部分报告，响应体带回 `run_id` 以便取轨迹。

## 4. 本地启动

需要 Go 1.23+。SQLite 使用纯 Go 驱动 `modernc.org/sqlite`（无 cgo 依赖）。
依赖已在 `go.sum` 锁定，可离线构建（`GOPROXY=off`）。

```bash
# 构建
go build ./...

# 跑全部测试（离线）
GOPROXY=off go test ./... -v

# 离线回放单个夹具（报告 JSON 到 stdout）
go run ./cmd/pvreplay testdata/fixtures/multi_exit.json
go run ./cmd/pvreplay -log logs/replay.log testdata/fixtures/oscillation.json

# 启动本地 HTTP 服务（默认仅监听 127.0.0.1:8080）
go run ./cmd/pvserver \
  -addr 127.0.0.1:8080 \
  -db data/pvserver.db \
  -fixtures testdata/fixtures \
  -log logs/pvserver.log
```

## 5. 示例请求

```bash
# 运行命名夹具
curl -s -XPOST 127.0.0.1:8080/v1/replays/fixtures \
  -H 'Content-Type: application/json' \
  -d '{"name":"multi_exit"}'

# 内联提交配置（见 examples/request.json）
curl -s -XPOST 127.0.0.1:8080/v1/replays \
  -H 'Content-Type: application/json' \
  --data-binary @examples/request.json

# 历史 / 报告 / 逐步轨迹 / 健康检查
curl -s 127.0.0.1:8080/v1/runs
curl -s 127.0.0.1:8080/v1/runs/<run_id>
curl -s 127.0.0.1:8080/v1/runs/<run_id>/trace
curl -s 127.0.0.1:8080/healthz
```

## 6. 夹具清单（全部为合成、本地、手工设计）

| 夹具 | 核验点 |
|------|--------|
| `multi_exit.json` | 双出口、IGP early-exit；撤回后逐跳切换到备选来源；传播链 AS_PATH |
| `policy_asymmetry.json` | 导入 local-pref 与导出拒绝相互独立；导出拒绝逐条邻居留证 |
| `withdraw_alternate.json` | MED（相邻 AS 相同）选路；撤回 p1 只删 p1，p2 提升；下游 RIB-Out 替换轨迹与水平分割 |
| `loop_suppression.json` | AS_PATH 含本 ASN 在导入策略之前被拒 |
| `oscillation.json` | 周期性上下线被状态签名证明振荡，返回循环证据 |
| `budget.json` | 长链传播在预算内未收敛，`budget_exceeded` 区别于振荡 |
| `unknown_withdraw.json` | 撤回未知来源 = `state_conflict`，其他来源保留 |
| `queue_cap.json` | 星形扇出打爆队列上限 = `resource_exhausted` |
| `invalid_input.json` | 引用不存在路由器 = `invalid_input` |

测试期望值（最佳路径来源、AS_PATH、MED、轨迹消息序列、循环步号、错误类别）
均为**在测试中手写的常量**，不由被测实现生成。

## 7. 诊断材料

- 每次运行有时间序、抗碰撞的 `run_id`，同时出现在日志行（`run=... step=...`）、
  报告体与 SQLite 记录里，可据此重放问题；
- 报告含每一步：阶段（seed/propagate）、输入、环/导入/导出判断与命中规则名、
  判定后候选全集、最佳路径与决胜步骤、入队 UPDATE（含 local-pref/MED/
  AS_PATH 快照）、导出拒绝的 `邻居:规则`；
- 运行脚本 `scripts/run_fixtures.sh` 会把每个夹具的完整报告与日志保存到
  `testdata/out/<时间戳>/`。

## 8. 关键取舍（明确的简化）

1. **只接受合成事件**：没有 TCP/线路 BGP、无计时器、无 OPEN/keepalive；
   处理模型是严格 FIFO、每个种子事件后排空队列。
2. **单 AS_SEQUENCE**：不实现 AS_SET、社团（community）、路由刷新等；
   ORIGIN 三值。
3. **iBGP 采用 next-hop-self**：不依赖外部 IGP 解析被保留的 eBGP 下一跳；
   IGP 代价直接取邻居（出口边界路由器）节点的 `igp_cost`。因此拓扑中的
   `address` 仅用于 eBGP 下一跳/展示。
4. **合成种子里"上游路由器"也是回放说话者**：它们同样会收到并处理传播来的
   UPDATE（受同样的环抑制约束）。这让夹具可端到端自洽，但不模拟真实运营商
   的策略黑盒。
5. **不做真实时钟**：振荡靠确定性状态签名的数学重现证明，而非 wall-clock。
6. **本地监听**：HTTP 默认只绑定 `127.0.0.1`，无鉴权（设计为本地工具）。
