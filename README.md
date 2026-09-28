# pvsim — 固定小型自治系统拓扑的路径向量收敛后端

一个**纯本地、只接受合成 UPDATE 事件**的 BGP 风格路径向量（path-vector / SPVP）收敛
回放后端。不连接任何公网邻居；拓扑、策略与事件全部来自提交的 JSON 场景文档，运行
结果与完整中间轨迹持久化在 SQLite 中，可按运行编号重放。

技术栈：**Go（标准库 net/http）+ 纯 Go SQLite（modernc.org/sqlite，无需 CGO）**。

---

## 1. 它验证什么（行为契约）

### 1.1 支持的选路属性与比较顺序

收到同前缀的多个邻居候选时，按以下**固定顺序**逐项比较，第一项分出胜负即停止：

| 顺序 | 比较项 | 偏好 |
|----|--------|------|
| 1 | `local_pref` | 越高越优 |
| 2 | `as_path` 长度 | 越短越优 |
| 3 | `med` | 越低越优，**仅当两条路径的首个（邻居）AS 相同才比较** |
| 4 | `origin` | `igp` < `egp` < `incomplete` |
| 5 | 学习来源 | eBGP 学到的优于 iBGP 学到的 |
| 6 | 到 next-hop 的 IGP 代价 | 越低越优 |
| 7 | 邻居 router id（拓扑中的声明序号） | 越小越优 |
| 8 | 最早学到的版本号 | 越早越优（保证顺序是全序、结果确定） |

每次选路变化都会记录决定它的那一步（`reason`），如 `local_pref`、`as_path_length`。

### 1.2 AS_PATH 环抑制（含本 AS 即拒绝）

- **入向（接收方）**：收到的 UPDATE，其 `as_path` 若已包含本路由器 AS，记录
  `loop_rejected` 并丢弃，**不安装为候选、不影响当前最佳路由**。
- eBGP 导出时在路径最前补本 AS；iBGP 不补 AS。
- **iBGP 水平分割**：iBGP 学到的路由不向其来源 iBGP 对等体再通告。eBGP 不做
  发送方抑制（RFC 行为是照常发出、由接收方做环检测）。
- 场景若在**外部注入事件**里就直接给出含本 AS 的路径，解析期按
  `INPUT/LOOP_IN_EVENT` 拒绝（避免夹具表达歧义）。

### 1.3 撤回只删除对应邻居候选

RIB-In 按 `(路由器, 前缀, 直连邻居)` 保存候选。WITHDRAWAL 只删除该邻居的候选：

- 撤回的是**非最佳**来源 → 不触发重选，最佳路由与其他来源候选原样保留；
- 撤回的是**最佳**来源 → 从保留下来的其他候选中重选（回退），并向邻居通告增量；
- 撤回一个本地从未持有过的来源 → 记 `withdraw_unknown`，什么都不清。

### 1.4 导入 / 导出策略独立应用

每条会话两端各有独立的有序规则链（`import_a/import_b`、`export_a/export_b`）：

- **首条匹配生效**：`deny` → 丢弃；否则按顺序施加动作并放行；无规则匹配 → 默认放行；
- 支持的匹配：`prefix`、`prefix_set`、`from_router`、`as_path_contains`、
  `as_path_equals`、`as_path_length_gt/lt`、`local_pref_gt/lt`、`med_gt/lt`、`origin`；
- 支持的动作：`set_local_pref`、`set_med`、`set_origin`、`prepend_as`；
- 导入策略决定**是否接收/如何改写入向路由**；导出策略决定**是否/如何向某个邻居通告**，
  两者互不影响——导出被拒绝不会删掉本地候选，导入被拒绝也不影响其他会话的导出。

属性语义：`local_pref` 是 iBGP-only 属性——eBGP UPDATE 自带的 local_pref 在策略
执行**之前**被剥离（但导入策略可以重新设置一个，设置值保留）；eBGP 导出时
local_pref 不离开本 AS。

### 1.5 振荡预算与循环证据

外部事件逐个注入（每个合成事件之间把网络排空到静止）。所有外部事件注入完毕后，
引擎对**完整确定性状态**（全部 RIB-In 候选 + 当前最佳 + 各有向边最近通告 + FIFO
待处理队列）计算签名：

- 队列排空且无更多外部事件 → **收敛**（固定点）；
- 同一完整状态签名在队列仍非空时重现 → 证明确定性重放陷入环，预算耗尽时返回
  `converged=false`、`non_convergent_code=OSCILLATION_BUDGET_EXCEEDED`，并给出
  `cycle`：入口版本、重现版本、环长度与两处最佳路由快照；
- 预算耗尽但尚未观测到签名重现 → `BUDGET_EXCEEDED_NO_CYCLE`（预算太小或非周期抖动）。

> 签名必须包含 RIB-In 与队列：仅对“最佳路由”做签名会把传播途中的瞬态稳定误判成环。

---

## 2. 工程组织（模块边界与错误契约）

```
model/      领域类型：Attrs、显式比较顺序 Compare、分类错误 model.Error
config/     JSON 解析(scenario.go) / 语义校验(validate.go) / 策略求值(policy.go)
engine/     SPVP 回放核心：FIFO 队列、候选 RIB、选路、导入导出、环抑制、签名检测
store/      SQLite 持久化：runs / deliveries / decisions / traces
replay/     编排：解析→执行→事务持久化→查询/重放；错误类别到 HTTP 的映射
internal/oracle/ 独立参考求解器：同步轮询不动点算法（不 import engine，比较/策略
            与传播均另行实现），用于交叉验证被测引擎
api/        本地 HTTP 接口
cmd/pvsim/  启动入口
fixtures/   四个合成夹具（多出口 / 策略不对称+撤回 / 策略振荡）
e2e/        端到端测试：夹具结果 + 引擎 vs 独立 oracle 交叉验证
```

**数据契约**：跨包只传 `config.Scenario`、`engine.*`、`model.Attrs` 等不可变领域
结构；原始 JSON DTO 不外泄。引擎通过 `engine.Sink` 接口产出中间物，回放层接
SQLite、测试接内存收集器，因此引擎不依赖存储包。

**错误契约**（`model.Error{Kind, Code, Message}`，四类可区分）：

| Kind | 含义 | HTTP | 典型 Code |
|------|------|------|-----------|
| `INPUT` | 请求/配置输入错误，未执行 | 400 | `PAYLOAD_SYNTAX` `INVALID_CONFIG` `UNREFERENCED_ENTITY` `LOOP_IN_EVENT` |
| `STATE_CONFLICT` | 与服务端状态冲突 | 409 | `RUN_ID_EXISTS` |
| `RESOURCE_EXHAUSTED` | 预算/容量耗尽 | 422 | `PAYLOAD_TOO_LARGE` `MAX_STEPS_CAP` |
| `COMPUTE_FAILED` | 内部不变量/持久化失败 | 500 | `PERSISTENCE` `EVENT_LOOKUP` |
| `NOT_FOUND` | 运行不存在 | 404 | `RUN_NOT_FOUND` |

未收敛**不是错误**：它是被正常持久化的结果（HTTP 201，`converged:false` +
`non_convergent_code` + `cycle`）。

---

## 3. 本地启动

需要 Go 1.23+（依赖锁定在 go1.23 兼容版本；纯 Go，免 CGO）。

```bash
# 依赖已在 go.mod / go.sum 锁定
go build ./...
go test ./...                 # 全部测试（含 -race 见下）
go test -race -count=1 ./...  # 竞态检测

# 启动（默认仅监听回环 127.0.0.1:8080，数据库 ./pvsim.db）
go run ./cmd/pvsim -addr 127.0.0.1:8080 -db ./pvsim.db
# 或
go build -o bin/pvsim ./cmd/pvsim && ./bin/pvsim -addr 127.0.0.1:8080 -db ./pvsim.db
```

数据库用 `:memory:` 可得到不落盘的临时库。后端只监听回环，且从不主动外连。

### 依赖锁定

`go.mod` / `go.sum` 已提交。SQLite 驱动固定为 `modernc.org/sqlite v1.36.1`
（其更新版本要求 Go 1.25；选此版本以在 Go 1.23 工具链上零工具链下载构建）。
如需离线可设置 `GOFLAGS=-mod=readonly`，并保证模块缓存中已有 go.sum 所列版本。

---

## 4. HTTP 接口与示例请求

| 方法与路径 | 作用 |
|-----------|------|
| `POST /runs` | 提交 `{"run_id"?:"...", "scenario":{...}}`，执行并持久化，返回结果 |
| `GET  /runs` | 列出最近运行（状态、是否收敛、步数、时间） |
| `GET  /runs/{id}` | 运行摘要 + 完整结果（各路由器最佳路由） |
| `GET  /runs/{id}/traces` | 有序中间轨迹（注入/拒绝/候选/选路/通告/撤回） |
| `GET  /runs/{id}/decisions` | 每次最佳路由变化及决定理由 |
| `GET  /runs/{id}/deliveries` | 外部事件按交付版本的注入顺序 |
| `GET  /runs/{id}/scenario` | 当初提交的**原始场景字节**（用于精确重放） |
| `POST /runs/{id}/replay` | 用存储的场景重新跑一遍，生成**新的** run id |
| `GET  /healthz` | 存活检查 |

```bash
# 提交夹具 A（多出口选路）
curl -s -X POST http://127.0.0.1:8080/runs \
  -H 'Content-Type: application/json' \
  --data-binary @<(printf '{"scenario":'; cat fixtures/01_multi_exit.json; printf '}')

# 指定 run id 提交振荡夹具，查看循环证据（examples/request.sh 安全包裹 JSON）
RID=$(examples/request.sh POST fixtures/03_oscillation.json osc-demo | python3 -c 'import sys,json;print(json.load(sys.stdin)["run_id"])')

# 看轨迹与决策
curl -s http://127.0.0.1:8080/runs/$RID/traces   | python3 -c 'import sys,json;[print(t["version"],t["category"],t["router"],t["peer"],t["detail"]) for t in json.load(sys.stdin)["traces"] if t["category"] in ("loop_rejected","best_changed")]'
curl -s http://127.0.0.1:8080/runs/$RID/decisions

# 精确重放（新 run id）
curl -s -X POST http://127.0.0.1:8080/runs/$RID/replay
```

> 手工把场景文件嵌进信封可用
> `python3 -c 'import json,sys;print(json.dumps({"scenario":json.load(open(sys.argv[1]))}))' fixtures/01_multi_exit.json`，
> `examples/request.sh` 封装了这一步。

### 最小场景文档

```json
{
  "name": "demo",
  "max_steps": 500,
  "routers": [
    {"name": "r9", "asn": 65009},
    {"name": "r1", "asn": 65001}
  ],
  "sessions": [
    {"id": "s91", "a": "r9", "b": "r1", "type": "ebgp",
     "import_b": [
       {"name": "prefer",
        "match": {"prefix": "203.0.113.0/24"},
        "actions": [{"type": "set_local_pref", "set_local_pref": 200}]}
     ]}
  ],
  "events": [
    {"seq": 1, "router": "r1", "peer": "r9", "kind": "update",
     "prefix": "203.0.113.0/24",
     "attrs": {"as_path": [65009], "med": 10, "origin": "igp"}}
  ]
}
```

`as_path` 采用 BGP 约定，最前为最近经过的 AS；空路径表示本 AS 起源。

---

## 5. 合成夹具与验证材料

| 夹具 | 验证点 |
|------|--------|
| `fixtures/01_multi_exit.json` | **多出口 + 比较顺序**：r3 有 r1/r2 两个出口；r1 路径被故意 prepend 加长，r2 路径本会因更短 AS_PATH 胜出，但 r3 的导入策略给 r1 路径 local_pref=200，最终 r1 逆转胜出（理由 `local_pref`）。同时 r2 坚持自己更短的 eBGP 直连。 |
| `fixtures/02_withdraw_policy.json` | **策略不对称 + 撤回替代**：r1 的导出策略禁止向 r3 通告该前缀（r3 只能经 r2 学到）；r9 在 r1 处撤回后，r1 回退到保留的 r2 来源候选，随后 r2 路径缩短。断言回退决策 `r9 -> r2`、export_denied 轨迹。 |
| `fixtures/03_oscillation.json` | **策略振荡（bad gadget）**：a/c/b 的链式偏好构成无稳定解的策略环；引擎返回未收敛 + 循环证据（入口/重现版本、环长度、快照），独立 oracle 也独立判定非收敛。 |

另含 `engine` 层夹具式单测：入向环抑制、eBGP 环最终落定、撤回隔离与回退、
导入/导出独立性、撤回后重新通告、导出动作改写、硬预算上限（`MAX_STEPS_CAP`）、
极小预算（`BUDGET_EXCEEDED_NO_CYCLE`）。

### 独立测试如何避免“自测自证”

- **预期结果为人工编写的具体断言**（哪台路由器经哪个邻居、决定理由、轨迹类别、
  错误类别与 HTTP 码），不是从引擎输出生成；
- 另设 **`internal/oracle` 独立参考求解器**：用与引擎不同的**同步轮询**算法
  （冻结 RIB-In 快照→各自重选→集中交换→下一快照，直到不动点/签名重复），且
  不 import engine；其比较、策略匹配、传播全部重写。`e2e` 测试要求两个实现对
  核心拓扑的最终选路一致、对振荡夹具都判非收敛。
  - 已知的**执行模型差异**（非选路分歧）：异步引擎会把某台路由器的瞬态最佳路由
    反射给存根 AS，而同步 oracle 只传播每轮落定后的最佳，故存根路由器（外部事件
    对等体）的最终反射状态不纳入交叉断言；核心路由器必须逐台一致。测试中已注明。

### 诊断信息（可重放）

每个运行持久化：唯一 `run_id`（时间有序，如 `run-20260927T184551-08bf3bca8b3f`）、
交付版本号、外部事件交付顺序、逐次选路决策（旧/新邻居、亚军、理由）、完整轨迹
（含策略拒绝与环抑制的原因与属性前后快照）、原始场景字节。凭 run id 取
`/scenario` 即可原样重放问题。

---

## 6. 支持范围与关键取舍

**支持**

- 固定、显式声明的小型拓扑；每对路由器至多一条 BGP 邻接；eBGP 与 iBGP 两类会话；
- 单前缀/多前缀、多来源（多出口）、每会话两端独立的有序导入/导出策略；
- 合成 UPDATE / WITHDRAWAL 的有序注入、增量传播、确定性选路与确定性重放；
- 路径环抑制、iBGP 水平分割、eBGP AS prepend、local_pref 域内语义、MED 仅在
  同邻居 AS 间比较；
- 振荡检测（完整状态签名）与两类未收敛结论；SQLite 持久化与按 run id 重放。

**明确不做（取舍）**

- **不接真实 BGP / 公网邻居、不做 TCP 长连接**：只回放合成事件，便于确定性测试；
- 不实现 TCP-MD5、keepalive/hold-timer、能力协商、UPDATE 打包分段、route-reflector
  /联邦、ADD-PATH、communities/extended communities（除文档列出的属性外，未知字段
  在解析期直接拒绝）；
- 前缀是不透明标签（按相等比较），不做最长前缀匹配与地址族计算；
- 每对路由器只允许一条会话（多会话模型超出范围）；IGP 代价为会话上的静态声明值，
  不运行 IGP；
- 注入模型为“逐事件注入、事件间排空”，不模拟任意消息延迟/丢失/重排的调度器；
- 选路最终 tie-break 用拓扑声明序号作为 router id（确定性替代真实 router-id 选举）。

这些取舍都服务于目标：一个小而确定、错误类别清晰、结果可重放核验的收敛后端。
