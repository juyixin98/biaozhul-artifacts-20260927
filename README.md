# netpolicy — 受限容器网络策略离线可达性判定后端

对一个小端点集合，离线判定“源端点 → 目标端点（协议/端口）”在
NetworkPolicy 风格策略模型下是否可达，并给出**允许或阻断的策略来源**。
无真实集群、无生产账号：所有期望状态来自本地合成 JSON 夹具，状态持久化在
本地 SQLite，判定服务用 Go 标准库 `net/http` 暴露。

## 判定模型（四条硬边界）

1. **入口、出口分别合成允许集合，连接需两侧同时允许。**
   每条策略按方向（Ingress 选目标、Egress 选源）和 `policyTypes` 独立求值；
   最终 `ALLOW` 当且仅当入口侧允许 **且** 出口侧允许。任一侧被隔离且无规则
   命中即 `DENY`，响应分别用 `ingress_selected_no_rule_matched` /
   `egress_selected_no_rule_matched` / `both_sides_selected_no_rule_matched`
   指明阻断来自哪一侧。

2. **未被任何策略选中的工作负载，默认行为明确：双向默认允许。**
   “未选中”按方向独立判定。空规则列表的策略会把被选中端点隔离为默认拒绝；
   没有任何策略选中的端点则默认放行。空策略 `{policyTypes:[Ingress,Egress]}`
   即双向默认拒绝（见 `empty-policies` 夹具）。

3. **命名端口按目标端解析，绝不当全局数字端口。**
   规则里的 `"http"` 在求值时对照**本次连接的目标端点**的端口声明解析：
   同一名字在 server1 是 TCP/80、在 server2 是 TCP/8080，二者不等价。
   目标端根本不提供该名字时按不命中处理；同一名字在目标端解析到多个不同
   （协议,端口）时判定为 `UNDECIDABLE/named_port_ambiguous`，绝不猜测。

4. **标签快照与策略版本一致。**
   每次协调把“名称空间 + 端点标签 + 策略”整体写入一个带单调版本号
   （revision）的不可变快照，事务提交。判定引擎从一个快照深拷贝构建，
   构建后对原数据的修改无法影响已建引擎；请求可用 `pinRevision` 固定版本，
   版本不符返回 `UNDECIDABLE/revision_conflict`，不会跨版本混用标签与策略。

## 判定结果三态

| Verdict       | 含义 |
|---------------|------|
| `ALLOW`       | 两侧都允许（含某侧未被选中而默认允许，reason 会说明） |
| `DENY`        | 至少一侧被策略隔离且无规则命中，reason 指明是哪一侧 |
| `UNDECIDABLE` | 无法判定：未知端点、不支持的协议、端口越界、版本固定冲突、命名端口歧义 |

每条结果都带 `ingress` / `egress` 两侧的追踪：`isolated`、`selectedPolicies`
（隔离来源）、`matches`（命中的策略名+规则下标）、`hints`（差一点命中的
原因）、`ambiguities`（无法判定的原因）。

## 目录结构（模块各有真实职责）

```
cmd/server/          HTTP 服务入口（装配、初始协调、优雅退出、版本跟踪）
cmd/netpolctl/       离线 CLI：validate / check / matrix（无需起服务、无需 DB）
internal/domain/     资源模型：命名空间/端点/标签选择器/策略/快照/校验与错误类别
internal/engine/     判定核心：双向合成、命名端口按目标解析、矩阵、快照深拷贝
internal/source/     适配器：合成 JSON 夹具 -> 校验+归一化+内容哈希（替代 informer）
internal/reconcile/  协调循环：Fetch->保存->分类记账（applied/unchanged/各类失败）
internal/store/      SQLite 持久层：版本快照（事务）+ 协调运行记录（审计）
internal/adapter/    net/http 适配器：路由、请求校验、判定日志、版本固定
internal/diag/       结构化日志、请求关联 ID、敏感标签脱敏
internal/config/     JSON 配置 + 环境变量覆盖
test/fixtures/       合成夹具（scenarios 正常 / failures 故障）
test/fixtures/*.golden.json  手工推演的期望矩阵/判定（非被测代码生成）
test/oracle/         独立预言机：不 import engine，另写一份判定算法
test/golden/         三方比对：手工 golden ↔ 引擎 ↔ 独立预言机
test/failure/        故障类别、协调分类、版本存储的独立测试
configs/             配置文件
```

## 快速开始

需要 Go 1.23+（仅标准库 + 一个纯 Go 的 SQLite 驱动，无需 CGO）。

```bash
go test ./...                  # 运行全部独立测试
go build ./...                 # 构建
go run ./cmd/server -config configs/config.json
```

服务默认监听 :18080（可用 `NETPOL_HTTP_ADDR` 覆盖），启动时做一次协调并写入
SQLite，然后按 `reconcileInterval` 轮询夹具，也可 `POST /internal/refresh`
立即触发。

### CLI（最快的验证方式，无需服务/数据库）

```bash
go run ./cmd/netpolctl validate --fixture test/fixtures/scenarios/named-ports.json
go run ./cmd/netpolctl check    --fixture test/fixtures/scenarios/named-ports.json \
    --from u-client --to u-server1 --port 80 --protocol TCP
go run ./cmd/netpolctl matrix   --fixture test/fixtures/scenarios/overlapping-selectors.json \
    --port 8080 --protocol TCP
go run ./cmd/netpolctl matrix   --fixture test/fixtures/scenarios/named-ports.json --all-declared-ports
```

### HTTP 调用

```bash
# 单点判定（允许）
curl -s -X POST localhost:18080/v1/check \
  -H 'Content-Type: application/json' -H 'X-Request-ID: demo-1' \
  -d '{"sourceUid":"u-api","destUid":"u-web-a","protocol":"TCP","port":8080}'

# 穷举连通矩阵（所有有序端点对）
curl -s -X POST localhost:18080/v1/matrix \
  -d '{"protocol":"TCP","port":8080}'

# 对每个“端点声明过的端口”各扫一张矩阵（覆盖命名端口的真实数字）
curl -s -X POST localhost:18080/v1/matrix -d '{"allDeclaredPorts":true}'

# 固定策略版本（版本不符 -> UNDECIDABLE/revision_conflict）
curl -s -X POST localhost:18080/v1/check \
  -d '{"sourceUid":"u-api","destUid":"u-web-a","protocol":"TCP","port":8080,"pinRevision":1}'

curl -s localhost:18080/v1/status
curl -s localhost:18080/v1/snapshots
curl -s localhost:18080/v1/snapshots/1
curl -s -X POST localhost:18080/internal/refresh   # 立即重新协调
```

完整 API 见 [docs/api.md](docs/api.md)。

## 夹具与“非自证”的独立测试

正常场景夹具（含交叠选择器、空策略、命名端口、单向允许、policyTypes 缺省）：

- `overlapping-selectors`：6 端点、交叠 ingress 选择器（app=web 与
  role=frontend 交叠）、跨命名空间、空 `{}` peer、双向墙。
- `named-ports`：同名 `http` 在不同端点解析为不同数字；UDP 命名端口；
  目标不提供该名字的情形。
- `one-way`：只有 egress 策略，`a→b` 允许而 `b→a` 被出口侧阻断。
- `empty-policies`：空规则策略隔离双向，未选中端点默认放行。
- `policy-defaults`：省略 policyTypes 的缺省规则（只入 ing、不入 egress）。
- `named-port-ambiguous`：同名端口解析到多个数字，判 UNDECIDABLE。

测试不是“接口能调通”级别：

- 每个 `*.golden.json` 是**人工从夹具推演**的期望矩阵/判定与原因码；
- `test/oracle` 是**不导入 engine 的独立第二实现**；
- `test/golden` 对每个小端点集合做三方比对（手工答案 ↔ 被测引擎 ↔ 独立
  预言机），并断言逐格 Verdict、精确原因码、矩阵的穷举性与确定性；
- `test/failure` 对每类坏夹具断言**具体错误类别**（重复 UID、未知命名空间、
  端口越界、非法选择器算符、JSON 语法错、文件缺失），并验证协调运行被分类为
  `applied/unchanged/fetch_failed/validation_failed`、版本单调与历史裁剪。

开发过程中这些测试实际抓到并修复了多个真 bug（见下方“已验证记录”）。

## 诊断与脱敏

- 每个请求有 `X-Request-ID`（传入则沿用，否则生成），响应头与错误体回带；
  判定日志以 JSON 输出 requestId、revision、源/目标、端口、verdict、reason、
  两侧隔离标记、命中的策略名与规则下标、hints/ambiguities，说明**为什么**
  接受、拒绝或无法判定。
- 标签**值**不进入判定日志。标签写入快照/日志前可经
  `diag.RedactLabels`，键名含 secret/password/token/credential/auth/private/
  apikey 等的值统一打码为 `***REDACTED***`（有测试验证不泄漏、且不修改原 map）。

## 配置

`configs/config.json`，支持环境变量覆盖：`NETPOL_HTTP_ADDR`、
`NETPOL_FIXTURE_PATH`、`NETPOL_SQLITE_DSN`、`NETPOL_RECONCILE_INTERVAL`、
`NETPOL_LOG_LEVEL`、`NETPOL_HISTORY_KEEP`。

## 依赖（已锁定）

- Go 1.23，纯标准库 HTTP；
- `modernc.org/sqlite v1.36.1`（纯 Go、无 CGO）及其传递依赖，版本见 `go.mod`/`go.sum`。

## 已真实验证

`go test ./...` 全绿；并用真实 HTTP 服务端到端验证：四种判定、矩阵 6×6、
版本固定、在线协调版本演进（rev1 判 DENY → 换夹具 rev2 判 ALLOW、旧版本仍
可取、固定旧版本给 conflict）、坏夹具不污染在服版本（停留在 rev2 且
validation_failed 被记账）、命名端口按目标解析与歧义三态、CLI 各命令。

测试在开发中真实抓到并修复的缺陷：未隔离侧被错误默认拒绝；egress 命名端口
错误按源端解析；快照内容在版本号赋值前序列化导致历史 revision 归零；引擎未
深拷贝快照、构建后可被外部修改污染；协调记录 finishedAt 为零值、run id 未
回写；重复内容哈希仅与上一版本比较的幂等漏洞。

## 剩余限制

- 只建模 TCP/UDP；SCTP 等返回 UNDECIDABLE。不做 IPBlock/except、FQDN、
  多端口区间、多 IP/多网卡端点、IPv6 地址族策略。
- 每个端点按“一个 pod-like 实体 + 已声明端口”建模；不模拟 conntrack 已建立
  连接的回程放行（与 k8s 节点实现有关），本模型只判“能否由源发起”。
- 协调为轮询/触发式文件读取，不是真 watch；并发模型为单写多读、快照级
  替换，适合离线小集合，不做大规模端点的增量索引。
- 无鉴权/多租户：`/internal/*` 仅适用于本地受信环境。
- 命名端口歧义采取保守的 UNDECIDABLE（不替用户猜数字）。
