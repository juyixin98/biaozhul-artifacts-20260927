# fwrule — 离线首匹配网络访问规则语义分析

对**有序、首匹配（first-match）**的网络访问规则做离线语义分析：检测
**完全遮蔽（FULL_SHADOW）、部分遮蔽（PARTIAL_SHADOW）、冗余（REDUNDANT）**
和**空匹配（EMPTY_MATCH）**，并为每条诊断给出**具体报文见证（witness）**
或**精确覆盖分区（cover partition）**。默认动作（allow/deny）参与判定；
IPv4 与 IPv6 分开建模；未知协议显式处理。

分析不是文本前缀比较，而是在报文坐标空间
`(family, protocol, src IP, dst IP, src port, dst port)` 上做精确的
区间/CIDR 划分（partition），每个划分单元对"哪些规则匹配它"是齐性的，
因此一个单元只需求一次首匹配即可代表整块空间。

---

## 1. 模块布局（多模块后端，各模块承担实际工作）

| 模块 | 职责 |
|---|---|
| `internal/config` | 配置解析与严格校验：协议名、CIDR、端口区间、动作、默认动作、v4/v6 跨族空匹配 |
| `internal/netmodel` | 网络模型：128 位无族址地址、CIDR 二叉划分、端口区间划分、协议、匹配盒、报文 |
| `internal/analyzer` | 核心机制：按 `(族, 协议)` 建组 → 地址/端口划分 → 枚举齐性单元 → 首匹配/次匹配 → 诊断与证据 |
| `internal/store` | SQLite 状态存储：策略版本、分析报告、逐请求回放日志（含完整 trace） |
| `internal/replay` | 运行时回放引擎：严格首匹配，逐规则位置输出可解释追踪 |
| `internal/httpapi` | 本地 HTTP/JSON 接口，请求身份（X-Request-ID）贯穿响应与日志 |
| `cmd/server` | 可运行服务入口 |
| `cmd/analyze` | 离线分析 CLI（无需服务/数据库） |
| `tests/oracle` | **独立参考实现（oracle）**：逐包线性暴力扫描，刻意不共享分析器的几何代码 |
| `tests/analyzercheck` `tests/replaycheck` `tests/integration` | 独立测试：穷举比对、规则交换、默认拒绝、端到端 HTTP |

核心机制没有任何硬编码演示：规则全部来自 JSON 配置，几何与枚举对任意
规则集通用。

## 2. 依赖清单

- Go 1.23+（仅标准库 `net/netip`、`net/http`、`encoding/json` 等）
- SQLite 驱动：**`modernc.org/sqlite`（纯 Go，无 cgo，无系统依赖）**
- 演示脚本需要 `curl` 与 `jq`（仅脚本需要，服务本身不需要）

`go.mod` / `go.sum` 固定了版本。离线环境可在有网处
`go mod download` 后整体拷贝。

## 3. 快速开始

```bash
# 离线分析（不启动服务）
go run ./cmd/analyze configs/demo-policy.json | jq '.diagnostics[] | {rule_id,kind}'

# 一键本地演示：构建 -> 离线分析 -> 起服务 -> 上传策略 -> 回放见证报文 -> 查日志
./scripts/run_demo.sh

# 全部测试（含 -race）
./scripts/run_tests.sh
```

启动服务：

```bash
go build -o bin/fwrule-server ./cmd/server
./bin/fwrule-server -addr 127.0.0.1:8080 -db ./data/fwrule.db -policy configs/demo-policy.json
```

## 4. HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `POST /v1/policies` | 上传策略，生成新版本号，返回分析报告 |
| `GET  /v1/policies` | 列出版本 |
| `GET  /v1/analyze?version=N` | 对某版本（默认 latest）重新分析并持久化 |
| `POST /v1/replay?version=N` | 回放一个报文，返回决策与逐位置 trace，并落日志 |
| `GET  /v1/logs?limit=` | 回放日志列表 |
| `GET  /v1/logs/{request_id}` | 按请求身份取完整解释（版本、动作、trace） |

每个请求都带 `X-Request-ID`（未提供则自动生成 `req-…`），响应头回显，
回放日志以它为主键——接口结果与日志可按请求身份相互关联。

上传策略请求体：

```json
{ "name": "demo", "spec": { "name": "demo", "default_action": "deny",
  "rules": [ {"id":"r1","action":"allow","protocol":"tcp",
    "src_cidr":"10.0.0.0/8","dst_cidr":"0.0.0.0/0",
    "src_port":"1024-65535","dst_port":"443"} ] } }
```

回放请求体：

```json
{ "request_id": "w1", "protocol": "tcp",
  "src_ip": "10.0.0.1", "dst_ip": "192.168.1.1",
  "src_port": 2000, "dst_port": 443 }
```

## 5. 策略文件格式

```json
{
  "name": "demo-policy",
  "default_action": { "ipv4": "deny", "ipv6": "deny" },
  "rules": [
    {
      "id": "r01-web-allow",
      "action": "allow",
      "protocol": "tcp",
      "src_cidr": "10.0.0.0/16",
      "dst_cidr": "192.168.1.0/24",
      "src_port": "any",
      "dst_port": "80-443"
    }
  ]
}
```

- `default_action`：`"deny"`/`"allow"`（两族相同），或
  `{"ipv4":"deny","ipv6":"deny"}` 分别配置。**必须提供**；只配置了 v4
  时，v6 报文走到默认动作会得到显式错误而不是被猜测。
- `protocol`：`tcp`/`udp`/`icmp`/`icmpv6`/`gre`/`esp`/`ah`/`sctp`/`igmp`，
  或数字 `0-255`，或 `any`。
  - **未知协议名（拼写错误）是硬错误 `UNKNOWN_PROTOCOL`**，不会悄悄变成宽规则。
  - **未知协议数字（如 99）被接受**，按"恰好是协议 99"精确建模，但该规则
    与触及它的决策都带 `UNKNOWN_PROTOCOL_NUMBER` 不确定标记。
  - 只有 `tcp`/`udp` 允许 `src_port`/`dst_port`，否则报 `PORT_NOT_ALLOWED`。
- 端口：`80`、`8000-9000`、`any`（0-65535）。
- 源/目的 CIDR 跨族（v4 对 v6）不是解析错误，而是 `EMPTY_MATCH`：规则
  合法但匹配集为空，永不可达、可安全删除。

## 6. 诊断语义（重要：精确含义）

规则按数组顺序评估，命中的第一条决定动作。对规则 `r`：

- **FULL_SHADOW（完全遮蔽）**：存在报文能匹配 `r`，但对其中每一个，都有
  **更早**的规则先命中。`r` 从未成为首匹配。→ 可安全删除。
- **PARTIAL_SHADOW（部分遮蔽）**：`r` 对部分报文是首匹配（仍可达），但其
  匹配集中另有一部分被更早规则抢走。→ 一般**不**可直接删。
- **REDUNDANT（冗余）**：`r` 可达（有报文它赢），但对它赢的**每个**报文，
  次匹配规则（runner-up）或默认动作会给出**相同动作**。删掉 `r` 行为不变。
  → 可安全删除。
- **EMPTY_MATCH（空匹配）**：匹配集可证为空（如 v4/v6 跨族）。→ 可删除。

注意：

1. 一条规则可同时是 PARTIAL_SHADOW + REDUNDANT（部分区域被抢，而它真正
   胜出的区域又与后继/默认同动作）。
2. **冗余是"单条删除"概念，不对并集封闭**：规则 A、B 同动作且互为对方的
   接管者时，各自可删，但不能同时删。测试逐条删除验证（见
   `tests/analyzercheck/property_test.go`）。
3. 默认动作参与判定：一条末尾 `deny` 规则若其胜出区本就落入默认 deny，则
   它是冗余的；默认 allow 时结论会翻转。
4. `any` 协议规则与具体协议规则在全局顺序内竞争；分析器对具体协议精确
   枚举，并对"只有 any 规则能看见的协议"选取代表协议（含一个无人命名的
   数字协议）来见证 any 规则的可达/冗余。

### 证据长什么样

- `witness`：一个**具体报文**（族、协议号、源/目 IP、源/目端口）以及
  `decided_by`（决定它的规则 id 或 `<default:deny>`）和动作。可以拿这个
  报文直接打 `/v1/replay` 复核。
- `partitions`：规则匹配集被切成的**精确矩形分区**
  `(src CIDR × dst CIDR × src 端口区间 × dst 端口区间)`，每区给出
  `packet_count`（该区报文数）、区内见证报文和实际决定者。完全遮蔽会列出
  "被谁抢走"的覆盖分区；冗余会列出"删后由谁同动作接管"的分区。列表按区
  大小降序，单诊断最多 100 个分区（`truncated_partitions` 标记截断），
  截断只影响展示，不影响穷举判定。

## 7. 错误语义（稳定错误码）

回放 `decision.status="error"` 时给出稳定 `error_code`：

| code | 触发条件 |
|---|---|
| `MISSING_PROTOCOL` | 请求未给协议 |
| `UNKNOWN_PROTOCOL` | 协议名不在本地注册表（拼写错误） |
| `INVALID_PROTOCOL` | 报文协议写成 `any`（报文必须是具体协议） |
| `INVALID_SRC_IP` / `INVALID_DST_IP` | IP 字面量非法 |
| `FAMILY_MISMATCH` | 源 v4、目 v6（不存在这样的报文） |
| `DEFAULT_NOT_CONFIGURED` | 无规则命中且该族未配默认动作 |
| `DUPLICATE_REQUEST_ID`（HTTP 409） | 回放请求 id 与已落日志冲突，拒绝覆盖 |

配置编译错误（CLI 退出码 2 / HTTP `POLICY_INVALID`）：`UNKNOWN_PROTOCOL`、
`PORT_NOT_ALLOWED`、`bad src_cidr`、`invalid action`、
`default_action is required`、`duplicate rule id` 等。

**不确定结论单列**：报告顶层 `uncertainties` 数组，回放决策的
`uncertainties` 字段，专门放 `UNKNOWN_PROTOCOL_NUMBER`、
`DEFAULT_NOT_CONFIGURED`、`REDUNDANCY_UNPROVABLE` 等无法给出完全确定结论
的情况，与确定诊断分开。

## 8. 证据与正确性是怎么验证的（不是"接口能调通"式测试）

1. **独立 oracle（参考答案不由被测核心生成）**：`tests/oracle` 独立实现首
   匹配——对缩小空间里的**每个具体报文**做线性规则扫描，完全不使用分析器
   的划分/几何代码；并用同一暴力枚举独立推导每条规则的 FULL/PARTIAL/
   REDUNDANT/removable 分类。
2. **缩小空间穷举比对**：`tests/testdata/mini-policy.json` 使用 /30、端口
   0-9 的小宇宙，测试枚举源/目地址 × 端口 × 协议（tcp/udp/icmp/gre），
   另加规则块**之外**的地址与端口以覆盖默认动作：
   - 分析器对每条规则的分类必须与 oracle 独立推导**逐类一致**；
   - 分析器判定**可删**的规则，删除后 oracle 重算全空间，报文→动作必须
     完全不变；判定**不可删**的规则，删除必须至少改变一个报文（双向）；
   - 每个诊断 witness 都用 oracle 复核其决定者与动作。
3. **交叉端口区间**：mini 策略含 `1-2` vs `0-1`、`1000-2000/1500-2500/
   1800-1900` 等交叉区间，断言精确的部分遮蔽与冗余类别。
4. **规则交换**：
   - 交换互不相交（不同协议）的规则，全空间决策必须不变；
   - 交换重叠且冲突（allow/deny）的两条规则，必须能被检测到行为翻转
     （对照组，证明测试有区分力）；
   - 生成的相邻交换夹具 `mini-policy-swapped.json` 上，分析器必须报出新的
     遮蔽关系（说明结论随顺序变化，而非按 id/文本下结论）。
5. **默认拒绝/默认允许**：单规则 + 默认 deny 夹具验证放行、默认兜底，以
   及 v6 未配默认时的显式错误；随机属性测试在 deny 与 allow 两种默认下都
   运行。
6. **随机策略属性测试**：固定种子生成 80 个随机策略（随机块、端口区间、
   协议含 any、动作、默认动作），对每个策略逐条规则比较分析器与 oracle
   的四类结论，并逐条做删除等价验证。
7. **端到端**：真实 SQLite + HTTP，上传→版本号→分析→回放→按 request_id
   取日志（含 14 步 trace、版本号）、重复 id 返回 409。

运行并查看结果：

```bash
go test -race -count=1 ./...
# 或
./scripts/run_tests.sh
```

## 9. 复现步骤汇总

```bash
go build ./...
go test -race -count=1 ./...          # 全部测试，实际执行并报告
go run ./cmd/analyze configs/demo-policy.json
./scripts/run_demo.sh                 # 起服务 + curl/jq 演示
```

## 10. 范围与边界

- 地址建模为无符号 128 位整数，v4 存低 32 位；v4/v6 划分彼此独立。
- 分析器对具体协议与 any 规则的竞争完整枚举；any 规则在"无任何规则命名
  的协议"上的结论通过代表性协议见证（含一个未被命名的数字协议），覆盖
  any-only 可达性与默认等价的冗余；若某族默认动作缺失，相关规则不会被误
  判为冗余，并给出不确定标记。
- IPv6 的穷举测试夹具未做逐地址枚举（空间虽同样可缩小，但 oracle 目前
  枚举 v4；v6 路径由分析器几何、解析与回放测试覆盖）。
