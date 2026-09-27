# flexhash — 本地流五元组到下一跳的弹性哈希路由服务

把一个网络流的**五元组**（源 IP / 源端口 / 目的 IP / 目的端口 / 协议 L4）
确定性地映射到一个**下一跳成员**，并在成员增减、权重调整、健康失效时
做到：

- 相同流在成员集合不变时映射固定；
- 增删成员**只搬迁受影响的桶**，不做全量重新取模；
- 权重按明确的整数舍入策略落地；
- 失效下一跳**立即**从转发中排除，恢复走**版本化**重分配；
- 明确区分**桶份额（结构所有权）**与**实际流量份额（含故障转移）**；
- 所有配置/健康变更写入 SQLite 追加事件日志，可纯日志重放并校验一致性。

技术栈：**Go 1.23 + 标准库 net/http + SQLite（modernc.org/sqlite 纯 Go 驱动，无 cgo）**。
所有数据均为本地合成夹具，仅使用环回地址，无需任何生产账号或真实业务数据。

---

## 1. 目录结构（按模块拆分）

```
cmd/flexhash/           服务入口：配置加载、存储打开、事件日志重放/引导、HTTP 服务
cmd/genflows/           确定性合成五元组夹具生成器（LCG，无随机种子）
internal/config/        配置解析与校验（独立 JSON 配置）
internal/flow/          网络模型：五元组、规范化、哈希输入的规范字节串
internal/hashring/      路由核心：整数份额、桶分配/迁移、版本化管理器、故障转移
internal/store/         SQLite 状态存储：快照、分配表、健康事件、追加事件日志、运行日志
internal/replay/        回放接口：纯事件日志重建 + 逐版本持久化分配一致性校验
internal/server/        标准库 HTTP 接口
internal/fherr/         四类错误分类法（输入/状态冲突/资源耗尽/计算失败）+ 不可用
internal/testlog/       可重放测试证据（运行编号、中间状态、判断理由、失败类别）
internal/oracle/        独立参考答案（测试专用，不 import 被测核心）
e2etest/                固定 2 万流集合的分布/迁移/边界/并发验收测试
configs/                独立配置文件（flexhash.json 示例、demo.json 联调）
testdata/flows/         固定合成流夹具（20000 条，种子 20260927）
testdata/runs/          测试运行证据（JSON 运行日志、runlog.db、测试报告）
scripts/demo.sh         一键本地联调脚本
vendor/                 锁定的全部依赖（可完全离线构建）
```

这不是单文件脚本：配置解析、网络模型、状态存储、回放接口、HTTP、路由核心
各自独立成包，接口为真实类型，无桩实现，无硬编码输出。

---

## 2. 依赖锁定 / 本地启动

### 依赖

- Go 1.23+
- SQLite 驱动：`modernc.org/sqlite v1.34.5`（纯 Go，**不需要 C 编译器**）
- 依赖已 `go mod vendor` 到 `vendor/`，并用 `go.sum` 锁定哈希；
  在无网络环境可直接构建：

```bash
go build -mod=vendor ./...
# 或联网环境
go build ./...
```

### 启动

```bash
# 1) 生成固定流夹具（已随仓库提供，可重复生成；字节确定）
go run ./cmd/genflows -n 20000 -seed 20260927 -o testdata/flows/flows.json

# 2) 构建并启动（环回地址，默认 127.0.0.1:8080，见 configs/flexhash.json）
go build -o bin/flexhash ./cmd/flexhash
./bin/flexhash -config configs/flexhash.json
```

首次启动会从配置文件引导 config **v1** 并写入 SQLite；之后重启
**以事件日志为准**重放重建（配置文件的成员变化不会覆盖已持久化拓扑，
拓扑变更请走 `POST /v1/config`）。若日志中持久化的 `bucket_count` 与配置
文件不一致，启动会以状态冲突拒绝——桶数是拓扑身份，不允许运行期改变。

一键联调（自动构建、起服务、发请求、关停）：

```bash
PORT=19080 ./scripts/demo.sh
```

---

## 3. HTTP 接口与示例请求

### 3.1 流查询（五元组 → 下一跳）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/lookup \
  -H 'Content-Type: application/json' \
  -d '{"src_ip":"10.1.2.3","src_port":51000,
       "dst_ip":"10.9.9.9","dst_port":443,"protocol":"tcp"}'
```

```json
{
  "bucket": 968,
  "owner": "hop-b",
  "chosen": "hop-b",
  "address": "127.0.0.1:9002",
  "failover": false,
  "config_version": 1,
  "health_revision": 0
}
```

协议支持 `tcp/udp/icmp`（也接受数字 `6/17/1`）。`owner` 是桶的结构属主；
当属主不可用而走故障转移时 `chosen` 与 `owner` 不同且 `failover=true`。

### 3.2 拓扑更新（版本 +1，返回搬迁桶）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/config -H 'Content-Type: application/json' -d '{
  "members": [
    {"id":"hop-a","address":"127.0.0.1:9001","weight":3,"healthy":true},
    {"id":"hop-b","address":"127.0.0.1:9002","weight":2,"healthy":true},
    {"id":"hop-c","address":"127.0.0.1:9003","weight":1,"healthy":true},
    {"id":"hop-d","address":"127.0.0.1:9004","weight":1,"healthy":true}
  ]}'
```

返回 `version`、`moved_buckets`、`moved_sample` 与新配额 `quota`。
版本必须连续（v+1），否则 `409 state_conflict`。

### 3.3 健康事件（立即排除 / 版本化恢复）

```bash
curl -s -X POST http://127.0.0.1:8080/v1/members/hop-b/health \
  -H 'Content-Type: application/json' -d '{"healthy":false}'
# -> health_revision: 1，hop-b 的流立即故障转移
curl -s -X POST http://127.0.0.1:8080/v1/members/hop-b/health \
  -H 'Content-Type: application/json' -d '{"healthy":true}'
# -> health_revision: 2，流按新版本回到结构属主
```

重复的同态转换返回 `409 state_conflict`（不产生空修订号）；
未知成员返回 `400 input_error`。

### 3.4 观测与回放

```bash
curl -s http://127.0.0.1:8080/v1/config          # 当前拓扑/版本/健康修订
curl -s http://127.0.0.1:8080/v1/assignments      # 每成员桶计数
curl -s http://127.0.0.1:8080/v1/shares           # 桶份额 vs 配置权重份额
curl -s http://127.0.0.1:8080/v1/replay/verify    # 纯日志重放并逐版本校验分配
curl -s http://127.0.0.1:8080/v1/replay/state     # 重放得到的最终状态
curl -s http://127.0.0.1:8080/v1/runs             # 持久化的测试运行记录
curl -s http://127.0.0.1:8080/healthz
```

---

## 4. 关键算法与明确取舍

### 4.1 两级映射与稳定性

```
flow 五元组 --FNV-1a--> 桶 b ∈ [0,B) --亲和度贪心--> 结构属主 owner[b]
```

- 五元组先规范化为 `src|sport|dst|dport|proto` 字节串再哈希，
  流 → 桶与成员**完全解耦**，成员变化不改变流所在的桶。
- 桶 → 成员用**全局降序亲和度贪心匹配**：枚举每个（空闲桶，仍有缺额成员）
  对，按亲和度降序、（成员 ID，桶号）升序决胜，依次接受。初始放置与
  迁移复用同一套机制，结果与 Go map 遍历顺序无关、跨平台确定。

### 4.2 整数舍入策略（Hamilton / 最大余数法）

1. `exact[i] = B * weight[i] / totalWeight`（浮点理想值，仅用于余数比较）；
2. `quota[i] = floor(exact[i])`；
3. 剩余 `B - Σfloor` 个桶按小数余数从大到小各发一个，**余数并列时按成员 ID
   升序**决胜（完全确定）。

保证：`Σquota = B`；每个成员 `|quota - exact| < 1`；权重 0 的成员配额为 0、
不持有任何桶。我们在运行期不改变 B（桶数是拓扑身份），因此 Hamilton 法
已知的“人口悖论”在本系统中不可能触发。

### 4.3 增删成员只搬受影响桶（规则 1）

迁移时：

- 保留所有幸存成员当前持有的桶（即使权重变化，先尽量保留）；
- 删除成员（或权重降到 0）的桶全部释放；
- 幸存成员若**超过新配额**，释放其对自己亲和度**最低**的盈余桶
  （与贪心填充“取最高亲和度”对称：成员保留自己最想要的桶）；
- 空闲桶用同一全局贪心规则补齐到新配额。

因此加成员时，移动的桶**全部流向新成员**；删成员时，**只有被删成员的桶
搬迁**；调权时移动桶数恰为配额净差。测试对每条都有逐桶断言。

### 4.4 失效立即排除，恢复版本化（规则 3）

- 健康是与配置版本正交的**健康修订号 `health_revision`**，每次真实翻转 +1。
- 查询是对**不可变快照**的无锁读取；健康事件 copy-on-write 发布新快照，
  因此标记下一跳失效后**下一次查询立即排除**，无需重建桶表。
- 结构所有权在健康事件中**冻结不动**；失效成员桶上的流走加权故障转移。
- 恢复是一次显式的新健康修订：流在该修订后回到结构属主，行为可回放、
  可审计。配置版本与健康修订都要求单调连续，跳跃/重复均为状态冲突。

### 4.5 桶份额 ≠ 实际流量份额（规则 4）

- **桶份额** = 成员结构性持有的桶数 / B，只由权重决定，与健康、与具体流
  集合无关。
- **实际流量份额** = 用一批真实五元组解析后，成员实际承载的流占比，取决于
  流哈希在桶上的经验分布，以及故障期间从失效成员转移过来的量。

`GET /v1/shares` 只报结构份额并显式注明区别；真实流量份额由
`e2etest` 用固定 2 万流集合计算。故障转移在健康正权重成员中按
`亲和度/权重` 最小选择，用 `big.Int` 交叉相乘精确比较（`s1*w2 < s2*w1`），
不引入浮点平台差异。

### 4.6 哈希质量取舍

亲和度**不是**直接对 `"b<桶>|m<成员>"` 取一次 FNV——FNV-1a 的末步是线性的，
对仅末字符不同的成员 ID（如 `b`/`d`）得分只相差常数，实测会在接管同一批
桶时产生约 40/60 的系统性偏斜。我们对桶、成员**分别**做 FNV，再用
splitmix64 雪崩混合器组合，使近名成员亲和度统计独立，同时保持纯标准库、
跨平台确定。该缺陷与修复均有失败→修复→通过的测试历史（见下）。

### 4.7 支持范围与不做的事

- 仅 tcp/udp/icmp 三类 L4 协议与 IP 字面量五元组；不解析隧道/负载协商。
- 仅环回/本地合成拓扑，不做真实健康探测（健康由 API 注入），不转发数据包。
- 桶数固定，不支持运行期改 B（改 B 等同于重建新拓扑，显式拒绝）。
- 单进程 SQLite（WAL），写操作串行化；面向本地验收，不是分布式多副本存储。
- 故障转移为**加权即时重选**（无连接），不维护每流粘性会话表。

---

## 5. 错误分类（可区分，而非“接口能调用”）

| 类别 | 触发示例 | HTTP |
|---|---|---|
| `input_error` | 非法 JSON、坏 IP/协议、重复成员 ID、负权重、全零权重、未知成员健康变更 | 400 |
| `state_conflict` | 配置版本跳跃/重复、重复健康翻转、双引导、重放日志为空、SQLite 锁/唯一约束 | 409 |
| `resource_exhausted` | SQLite 打开/磁盘/连接限制、写库失败 | 503 |
| `computation_failed` | 分配未饱和、配额不变量被破坏、重放与持久化分配不一致、事件损坏 | 500 |
| `unavailable` | 没有任何健康且正权重的下一跳 | 503 |

---

## 6. 测试：固定流集合、迁移比例、边界、并发

测试**不**只检查“接口能调用”，而是断言具体结果与失败类别。

固定集合：`testdata/flows/flows.json`，20000 条五元组，由确定性 LCG
（种子 `20260927`）生成，可重新生成且字节一致。

**独立参考答案**：`internal/oracle` 不 import 被测 `hashring`，独立用 FNV
重推亲和度、独立实现 Hamilton 配额、独立的顺序贪心分配，以及**全量重新
取模基线** `owner = members_sorted[hash(flow) mod n]` 和朴素加权槽位基线。
因此“参考答案”不是被测核心自己生成的。

运行：

```bash
go test -mod=vendor -count=1 ./...
go test -mod=vendor -race -count=1 ./...     # 竞态检测
go test -mod=vendor -count=1 -cover ./internal/... ./e2etest/...
```

### 实测关键结果（20000 流，B=1024）

| 变更 | 弹性哈希移动流 | 全量重取模基线移动流 | 结论 |
|---|---:|---:|---|
| 1:1:1 → 加一个成员（1:1:1:1） | 4996（25.0%） | 14997（75.0%） | 仅 1/4 流随新桶移动，且**全部**落到新成员 |
| 1:1:1:1 → 删除一个成员 | 4923（24.6%，全来自被删成员） | 14876（74.4%） | 只有被删成员的桶搬迁 |
| a:b:d 1:1:1 → 3:1:1 | 5216（26.1%） | 朴素加权重取模 13338（66.7%）；普通重取模移动 0 但**完全违背权重** | 权重被满足（a≈59.4% 流），且搬动远少于加权重取模 |

配额具体值：1:1:1 → `a=342,b=341,c=341`（余数并列归 a）；3:2:1（B=1024）
→ `512/341/171`；3:1:1 → `614/205/205`。流份额与桶份额在 ±3% 内一致。

边界与并发测试：

- **权重为零**：成员不持桶、不接流，配额精确断言；**全部权重为零**被拒为
  `input_error`。
- **全部失效**：所有流解析为“无可用下一跳”（`unavailable`），不返回假成员。
- **桶份额 vs 流量份额**：a 失效时结构配额不变，a 的流按 b:d≈1:1
  （实测 5682:6225，约 47.7/52.3）故障转移，b/d 自有流不二次移动。
- **并发配置读取**：`-race` 下 16 个无锁读协程 + 单写协程连续 40 个版本波次，
  读到的每个快照配额之和恒等于桶数、无空属主、无 panic。
- **回放一致性**：干净日志逐版本零 mismatch；直接 SQL 篡改一个桶属主后，
  `replay/verify` 必须报出该版本 divergence。

### 可重放的测试日志

每次验收测试在 `testdata/runs/run-<UTC时间>-<用例名>.json` 留下：

- **运行编号**（可引用/重放）；
- 每个关键**中间状态**（各版本配额、流份额、移动桶/流数、与基线对比）；
- 每条断言的**判断理由**；失败时带**失败类别**。

同一运行编号也写入 SQLite `run_logs` 表（`GET /v1/runs` 可查）。
最近一轮汇总见 `testdata/runs/test-report.txt`、`coverage.txt` 与
`server-boot-*.log`。

### 实际运行记录：失败与修复（如实保留）

- 一次真实失败：初版亲和度直接用单次 FNV 组合键，`TestBucketShareVsTrafficShare`
  记录到加权故障转移 b:d = **4882:7181（非预期的不均衡）**，判定
  `computation_failed`。根因是 FNV-1a 末步线性相关（§4.6）。改为“分别哈希 +
  splitmix64 混合”后，同一用例复测为 5682:6225（≈1:1）通过。该失败运行证据
  曾出现在早期 run 文件中；最终一致证据见 `testdata/runs/` 中保留的运行日志。
- 联调时机器上 `127.0.0.1:18080/18099` 已被其他本地进程占用，首次启动
  `bind: address already in use`，换用 19080 后成功（见
  `server-boot-1.log`、`server-boot-2-restart.log`）。

无“未执行却声称通过”的项；`fherr`/`oracle`/`testlog` 为支撑包无自身测试
（`[no test files]` / 0% 覆盖为预期），其行为经由使用它们的测试间接覆盖。

---

## 7. 配置文件格式

```json
{
  "listen_addr": "127.0.0.1:8080",
  "sqlite_path": "data/flexhash.db",
  "bucket_count": 1024,
  "members": [
    {"id": "hop-a", "address": "127.0.0.1:9001", "weight": 3, "healthy": true},
    {"id": "hop-b", "address": "127.0.0.1:9002", "weight": 0, "healthy": false}
  ]
}
```

约束：`bucket_count ∈ [1,1e6]`；成员 1..4096；ID 非空且唯一；地址需可被
`net.ResolveTCPAddr` 解析；`weight ≥ 0`（0 合法但不持桶/不接流）；
全体正权重之和必须 > 0。请求体上限 1 MiB，配置文件上限 4 MiB。
