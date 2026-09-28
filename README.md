# cidrcov — 最小无重叠 CIDR 覆盖服务

给定**允许集合**与**排除集合**，计算并返回与 `allow \ exclude`（集合差）
**严格等价**的最小、无重叠 CIDR 前缀列表。支持 IPv4 与 IPv6，全程使用
`math/big.Int` 做区间运算，**绝不枚举地址**（处理 `::/0` 这类 2¹²⁸ 空间也是
O(前缀数) 而非 O(地址数)）。

- 语言/运行时：Go 1.23（仅标准库 `net/netip`、`net/http`、`math/big` 等）
- 存储：SQLite，纯 Go 驱动 [`modernc.org/sqlite`](https://pkg.go.dev/modernc.org/sqlite)（**无 cgo**）
- 所有外部输入均为本地合成夹具；不需要任何真实账号或业务数据。

---

## 1. 目录结构（多模块后端，各司其职）

```
cmd/cidrcovd/            服务入口：装配配置/存储/API、优雅关闭
internal/
  config/                配置解析（JSON 文件 + 环境变量覆盖，拒绝未知键）
  netmodel/              网络模型：大整数区间代数（并/差）、贪心最小前缀分解
  ipparse/               CIDR/裸IP 解析：分类错误、主机位/4in6 显式建议
  engine/                编排：按族拆分 → 求差 → 分解 → 独立自校验 → 可解释追踪
  store/                 SQLite 状态存储（请求与结果持久化，支持回放）
  api/                   标准库 net/http：路由、请求ID、结构化日志、回放接口
test/refmodel/           独立参考实现（测试专用，与生产代码零共享）
testdata/requests/       最小数据夹具（正常/IPv6极值/混合族/空集/非法）
configs/service.json     示例配置
examples/curl.sh         服务调用示例
runlogs/                 已保存的可复核运行结果（测试输出、服务日志、示例响应）
```

`internal/engine/verify.go` 在每次计算后用**区间原语**独立重建并校验；
`test/refmodel/` 是另一套**完全独立**的参考实现（测试不通过被测核心生成答案）。

---

## 2. 核心算法与边界语义

### 区间代数（不枚举地址）

- 所有地址表示为 `[0, 2^width)` 内的 `big.Int`，IPv4 `width=32`、IPv6 `width=128`。
- `Union`：排序 + 单趟扫描，合并**重叠与相邻**区间（相邻必须合并，否则会产生
  本可成为一个父前缀的两段）。
- `Subtract`：两个已归一化有序区间集的单趟差集。
- `IntervalToPrefixes`：在当前游标处贪心取**同时满足**两个约束的最大块：
  - 跨度约束：`2^h ≤ 剩余长度`，`h = ⌊log₂(remain+1)⌋`；
  - 对齐约束：`h ≤ 游标地址的尾随零位数`（游标为 0 时取满宽度）。

  “最大可容纳对齐块优先”产出的就是**唯一规范的最少前缀覆盖**；跨多个不相交
  区间不会出现可合并兄弟，因为相邻区间早已在 `Union` 阶段合并。

### 前缀边界语义（固定、显式）

**一个前缀始终表示完整对齐块，包含网络地址与广播地址**，没有任何 `/31` 特例：

- `10.0.0.0/30` 恰好是 4 个地址 `{.0,.1,.2,.3}`；
- 排除广播地址 `10.0.0.3/32` 后剩 3 个地址
  （见 `TestPrefixBoundaryPolicy`）。

带主机位的输入（如 `10.0.0.5/24`）会被掩码规范化，并在响应 `advisories` 中
以 `host_bits_canonicalized` 单独提示，而不是静默处理。

### 与目标差集严格等价 & 无可合并兄弟

每次输出都经 `engine.verify` 用独立路径断言：

1. 每个前缀合法、对齐、互不重叠；
2. 由输出重建的区间集与 `allow\exclude` **双向差集皆为空**
   （`cover\target` 与 `target\cover` 都必须为空，不是单向包含）；
3. 不存在两个等长、相邻、同父对齐的“兄弟前缀”（否则它们应合并成一个更短前缀）。

---

## 3. 如何验证正确性（参考答案独立于被测实现）

`test/refmodel/` 与生产实现**不共享任何代码**，且采用不同算法：

- **缩小位宽穷举（dense bitset + 自顶向下 trie）**：
  - 3 位时穷举**所有** allow×exclude 组合（256×256 = 65,536 例）；
  - 4 位时穷举所有 allow 集合 × 8 种结构化排除形状；
  - 8 位 / 16 位各随机 20,000 / 3,000 例（含重叠、乱序、跨对齐边界区间）。
  参考 trie 的输出是**唯一规范最少覆盖**，因此逐前缀比较不仅验证集合相等，
  还严格证明**前缀数量最少、无重叠、无遗漏、无额外地址**。
- **真实 128 位极值（big.Int 结构化 trie）**：`BigTrieCover` 递归分类
  FULL/EMPTY/PARTIAL，不枚举地址。覆盖全空间、空集、打最低/最高地址、
  两端同时打洞、顶部 `/127`、两个相距极远的主机等极端情形。

测试断言的是**具体结果与失败类别**，而非“接口可调”：
- 手算已知答案逐字断言（见 `TestKnownAnswersV4/V6`），例如
  `0.0.0.0/0 − 10.0.0.0/8`、`/24` 打一个主机洞的精确 8 前缀形状；
- 每个非法输入断言稳定错误码（`invalid_syntax` / `bad_prefix_len`）、
  所在列表、**1 基位置**与产生位置 `ipparse.Parse`；
- `verify_negative_test.go` 构造重叠、未对齐、越界、多地址、缺地址、
  可合并兄弟等缺陷，证明运行时自校验**确实会拒绝**，不是橡皮图章。

---

## 4. 快速开始

需要 Go 1.23+。SQLite 驱动为纯 Go，**无需 gcc/cgo**。

```bash
# 离线可复现：依赖已在 go.mod / go.sum 锁定
go build ./...
go test ./...              # 全部测试
go test -race ./...        # 竞态检测
go vet ./...

# 启动（默认 127.0.0.1:8080，SQLite 文件在 data/）
go run ./cmd/cidrcovd -config configs/service.json
# 或使用 Makefile：make run
```

### 配置

`configs/service.json`：

| 字段 | 说明 | 默认 |
|---|---|---|
| `listen` | HTTP 监听地址 | `127.0.0.1:8080` |
| `db_path` | SQLite 文件（`:memory:` 为临时库） | `cidrcov.db` |
| `max_entries_per_list` | 每个列表最大条目 | `10000` |
| `shutdown_timeout_ms` | 优雅关闭超时 | `5000` |

环境变量覆盖（优先级高于文件）：`CIDRCOV_LISTEN`、`CIDRCOV_DB_PATH`、
`CIDRCOV_MAX_ENTRIES`、`CIDRCOV_SHUTDOWN_MS`；命令行 `-listen/-db` 优先级最高。
配置文件中出现**未知 JSON 键会直接报错**，防止拼写错误被静默忽略。

---

## 5. HTTP 接口

所有响应使用统一信封，携带 `request_id`、`versions`（service + algorithm）、
`data` 与独立的 `error`。请求可用 `X-Request-ID` 头或 body 中的 `request_id`
关联；未提供时服务端生成 `req-…`，并回写响应头。

| 方法 路径 | 说明 |
|---|---|
| `GET  /healthz` | 健康检查 |
| `GET  /version` | 服务与算法版本 |
| `POST /v1/cover` | 计算覆盖；body：`{"request_id"?, "allow":[...], "exclude":[...]}` |
| `GET  /v1/requests/{id}` | 读取持久化的原始请求与结果 |
| `POST /v1/replay/{id}` | 用当前引擎**重放**原始输入，报告 `matches_recorded` |
| `GET  /v1/requests` | 最近 50 条请求 |

状态码：`200` 成功或空结果；`400` 请求格式错误；`422` 条目非法（结果中
含分类 `failures`）；`404` 未知回放 id；`409` 请求 id 重复；`500` 持久化失败。

`data.status`：`ok` / `empty`（差集为空）/ `error`。非致命的不确定结论
（主机位被掩码、裸 IP 扩为主机前缀、4in6 重基、重复条目、排除项不与允许集
相交）放在独立的 `advisories` 中，绝不与致命 `failures` 混在一起。

### 调用示例

服务启动后执行：

```bash
BASE=http://127.0.0.1:8080 bash examples/curl.sh
```

最小请求（夹具 `testdata/requests/basic_v4.json`）：

```json
{ "request_id": "fixture-basic-v4",
  "allow": ["10.0.0.0/24"],
  "exclude": ["10.0.0.5/32"] }
```

返回 8 个最小前缀、覆盖 255 个地址，并带完整 `trace`
（start→parse→union→subtract→decompose→verify→finish，每步含 `location`
处理位置与 `detail`）。完整响应见 `runlogs/example_output.txt`。

---

## 6. 可解释性（请求身份 / 关键步骤 / 版本 / 失败单列）

- 每条日志为 JSON，带 `request_id`（以及重放时的 `replay_of`），可与响应、
  SQLite 记录三方关联，见 `runlogs/server.log`。
- 结果中 `trace` 列出关键步骤、产生代码位置（如 `netmodel.Subtract`、
  `engine.verify`）与每步数量；`algorithm_version`（`cover-greedy-v1`）、
  `service_version`、`go_version` 一并持久化。
- 致命问题在 `failures`（稳定 code + 列表 + 1 基 index + 原始输入 + 原因 +
  位置）；可接受但需注意的结论在 `advisories`，两者严格分开。

---

## 7. 依赖锁定与可复现性

- `go.mod` / `go.sum` 已提交，固定 `modernc.org/sqlite v1.34.5` 及其完整
  传递依赖；纯 Go，无需联网或 cgo（`GOPROXY=off go test ./...` 亦可）。
- 已保存的可复核结果：
  - `runlogs/test_results.txt` — `go test -v ./...` 全量输出（50 项全过）；
  - `runlogs/example_output.txt` — 真实服务上各夹具的端到端响应；
  - `runlogs/server.log` — 与请求 id 关联的结构化日志。

重新生成本地结果：

```bash
make test                 # 或 GOPROXY=off go test ./...
go build -o /tmp/cidrcovd ./cmd/cidrcovd
CIDRCOV_LISTEN=127.0.0.1:18080 CIDRCOV_DB_PATH=/tmp/demo.db /tmp/cidrcovd &
BASE=http://127.0.0.1:18080 bash examples/curl.sh
```

---

## 8. 边界与设计说明

- IPv4 与 IPv6 **自动按族拆分、各自独立求差与覆盖**，不会静默混族；
  IPv4-mapped IPv6 写法（`::ffff:10.0.0.0/120`）会重基到 IPv4 `/24` 并给出
  `ipv4_mapped_in_ipv6_rebased` 建议；映射前缀短于 `/96` 无法表达为低 32 位，
  判为 `bad_prefix_len`。
- 请求体上限 1 MiB；单列表条目超限返回 `entry_limit_reached`。
- 地址计数用十进制字符串承载（IPv6 计数超过 2⁶⁴）。
