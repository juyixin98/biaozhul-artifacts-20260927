# flowrouter — 本地五元组到下一跳的弹性哈希路由服务

把一条网络流的**五元组**（源 IP、目的 IP、L4 协议、源端口、目的端口）通过**加权一致性哈希**
映射到一个下一跳成员；成员增删只迁移受影响的桶；成员失效立即摘除，恢复走**版本化重分配**。
全部数据与外部参与者均为本地合成夹具（loopback 地址、本地 SQLite、固定种子流集合），
不依赖任何生产账号或真实业务数据。

技术栈：Go 1.23（标准库 `net/http`）+ SQLite（纯 Go 驱动 `modernc.org/sqlite`，无 cgo）。

---

## 1. 它满足的四条核心规则

1. **相同流在成员不变时固定映射；增删成员只搬迁受影响的桶。**
   环上的虚节点（vnode）位置只由 `(memberID, replicaIndex)` 决定，与其它成员无关。
   新增成员只插入新 vnode，落在新 vnode 弧段前的桶才换主；删除成员只把它名下的桶交给后继。
   测试对固定 300 / 10000 条流逐流断言，并与「全量重新取模 `hash % N`」基线对比：
   - 加成员（3→4）：一致性哈希迁移 **19.3%**，全量取模迁移 **74.3%**（10k 语料）；
   - 删权重成员：迁移 **34.0%** vs 基线 **66.0%**；
   - 纯权重调整：成员数不变时取模基线为 0（它根本无法表达权重），服务仍按 vnode 弧段精确迁移。

2. **权重分配与整数舍入有明确策略。**
   - 不封顶：`vnode 数 = 权重 × vnodes_per_weight`，整数精确乘法、无舍入。
   - 设上限 `max_vnodes`：两段式**最大余数法**——先给每个正权重成员保底 1 个 vnode，
     再把剩余 `cap - n` 个按权重比例取 floor，余下的按小数余数从大到小逐个发放，
     余数并列时按成员 ID 升序，保证确定性。总数恒等于 `cap`，且每个正权重成员至少 1 个。
   - 权重 0 的成员保留身份、可被健康/管理操作寻址，但不拥有任何 vnode、不接流量。
   - 每次分配的 `counts/base/extra/ideal/remainder/strategy` 都进入快照、API 和 SQLite，可审计。

3. **失效下一跳立即排除；恢复遵循版本化重分配。**
   - `POST /admin/members/{id}/down`（或探测连续失败达阈值）立即发布新一代，该成员不在新环上，
     之后没有任何流会再路由给它。
   - 探测**只能把成员标记为 down，永不自动标记为 up**，避免抖动引发迁移风暴；
     恢复必须显式 `POST /admin/members/{id}/up`，发布新版本并重分配。
   - 所有变更用单调版本号 + `expected_version` 乐观并发（CAS）；版本不符返回
     `409 STATE_CONFLICT/VERSION`。相同输入指纹不产生新版本（幂等）。
   - 确定性保证：成员输入回到与之前某代完全相同时，映射逐流恢复。测试断言
     「全部下线再恢复」的迁移数**恰好为 0**。

4. **严格区分桶份额与实际流量份额。**
   - `bucket_share = 该成员 vnode 数 / 环上总 vnode 数`，是**结构性**环弧份额；
   - `traffic_share = 在某个具体、有限流集合上实际落到该成员的流占比`，是桶份额在该语料
     哈希值上的蒙特卡洛实现，语料有限时会偏离，语料越大越收敛。
   - `/v1/version` 只报桶份额，`/v1/stats` 两者都报并附说明。固定夹具同时记录两份数值，
     用于明确展示二者差异（小语料上偏差可达约 20 个百分点）。

---

## 2. 模块划分（真实模块，非单文件脚本）

```
cmd/flowrouter/         启动、配置装载、SQLite 恢复、优雅关停
cmd/genfixtures/        固定种子生成流集合夹具（独立程序，非被测核心）
internal/apperr/        全服务错误分类法（5 类 + 稳定 code + HTTP 映射）
internal/config/        YAML 配置解析与校验（不碰套接字/环/存储）
internal/flow/          网络模型：五元组、规范化、CanonicalKey
internal/hashx/         哈希固定：FNV-1a64 + splitmix64 雪崩终结、版本化键空间
internal/ring/          加权整数分配、一致性哈希环、版本对比、取模基线
internal/router/        版本化引擎：原子快照、CAS、健康状态、计数器
internal/health/        可选 TCP 主动探测（只标记 down）
internal/store/         SQLite 持久化（配置修订、环版本、回放运行、决策、迁移）
internal/replay/        版本化回放：固定流集合在两代间的迁移/基线/失败落库
internal/api/           标准库 net/http JSON 接口
internal/testutil/      测试公共夹具
testdata/flowsets/      固定流集合（300 / 10000，seed=20260927）
testdata/scenarios/     成员/版本场景矩阵（Go 与 Python 共用）
testdata/oracle/        独立 Python 预言机 + 黄金参考文件
testdata/configs/       零权重等边界配置
configs/                示例配置
scripts/                示例请求脚本
```

---

## 3. 本地启动

前置：Go ≥ 1.23（构建/测试不需要 gcc，SQLite 驱动为纯 Go）；生成黄金参考需要 Python 3（仅测试用）。

```bash
go mod download          # 首次拉取，go.sum 已锁定精确版本
make fixtures            # 生成固定流集合（已随仓库提交，可跳过）
make oracle              # （可选）用独立 Python 预言机重新生成黄金参考
make run                 # 启动：127.0.0.1:8080，数据落 data/flowrouter.db
```

或直接：

```bash
go run ./cmd/flowrouter -config configs/config.example.yaml -flowsets testdata/flowsets -reset-db
# -check-config   只校验配置后退出（非法退出码 1）
# -reset-db       启动前删除数据库文件
# store.path 设为 ":memory:" 则为不落盘的临时库
```

服务首次启动把配置与**环版本 1**写入 SQLite；之后重启会从最新持久化版本**恢复**
（版本号延续，运行时 down 状态一并恢复）。

---

## 4. 示例请求

```bash
./scripts/example_requests.sh
```

关键端点（完整列表见 `internal/api` 包注释）：

```bash
# 解析单条五元组
curl -s -X POST localhost:8080/v1/route -H 'Content-Type: application/json' \
  -d '{"src_ip":"10.0.0.1","dst_ip":"10.1.0.9","proto":6,"src_port":40001,"dst_port":443}'

# 当前版本 / 成员 / 桶份额
curl -s localhost:8080/v1/version

# 桶份额 vs 实际流量份额
curl -s localhost:8080/v1/stats

# 立即摘除（expected_version 为乐观锁）
curl -s -X POST localhost:8080/admin/members/hop-a/down \
  -H 'Content-Type: application/json' \
  -d '{"expected_version":1,"reason":"manual-maintenance"}'

# 版本化回放：固定流集合在版本 1→2 的迁移，对比全量取模基线
curl -s -X POST localhost:8080/v1/replay -H 'Content-Type: application/json' \
  -d '{"flow_set":"flows_smoke","from_version":1,"to_version":2}'

# 查询某个运行（含失败分类与逐流迁移）
curl -s localhost:8080/v1/replay/<run_id>
curl -s localhost:8080/v1/runs
```

五元组 JSON：`src_ip,dst_ip` 为 IP 字面量（拒绝主机名，避免一名多址混淆）；
`proto` 用 IANA 协议号（6 TCP / 17 UDP / 132 SCTP）；非端口协议端口必须为 0。
IPv6 输入会被规范化，等价写法产生同一个 canonical key。

---

## 5. 错误分类（四类可区分 + 无健康成员）

| Kind | code 示例 | HTTP | 含义 |
|---|---|---|---|
| `INVALID_INPUT` | `BAD_SRC_IP` `MEMBER_DUPLICATE_ID` `BAD_JSON_BODY` `FLOWSET_*` `CAP_TOO_SMALL` | 400 | 输入/配置非法，调用方可修 |
| `STATE_CONFLICT` | `VERSION` `ALREADY_LOADED` `UNKNOWN_VERSION` `UNKNOWN_RUN` | 409 | 乐观版本冲突或引用不存在的版本/运行 |
| `RESOURCE_EXHAUSTED` | `STORE_BUSY` `STORE_IO` | 503 | SQLite 锁/IO 等本地资源暂不可用，可退避重试 |
| `COMPUTATION_FAILURE` | `CAP_BELOW_MEMBER_COUNT` `VNODE_COUNT_OVERFLOW` `ALLOCATION_MISMATCH` | 422 | 结构性计算失败（持久化分配与重建不一致等） |
| `NO_HEALTHY_MEMBER` | `NO_MEMBERS` `ZERO_TOTAL_WEIGHT` `ALL_DOWN` | 503 | 无可用下一跳，三类原因互相区分 |

错误统一为信封：`{"error":{"kind","code","message"},"request_id":...}`，
每个响应带 `X-Request-Id`，访问日志记录 request_id / 方法 / 路径 / 状态 / 耗时。

---

## 6. 回放与问题重放

每次回放都在 SQLite 留下：`replay_runs`（运行编号 `run-...`、输入流集合、起止版本、
状态 `RUNNING/COMPLETED/FAILED`、汇总、**错误 kind/code/message**）、`decisions`
（两个版本下每条流的归属）与 `migrations`（每条换主流的 key、哈希、原属/新属、原因类别）。
失败的回放同样落库且可 `GET` 取回，因此一个问题可以凭**运行编号 + 关键中间状态 + 判断理由**
被精确重放。迁移原因类别：`member_added` / `member_removed` / `weight_changed` /
`old_ring_empty` / `new_ring_empty`。

---

## 7. 测试策略（断言具体结果，不是“接口能调用”）

- **独立预言机**：`testdata/oracle/oracle.py` 是用**另一种语言（Python）从零写的**参考实现
  （独立的 FNV-1a+splitmix、独立的环/分配/迁移/取模逻辑），读取同一份场景矩阵与流集合，
  产出 `golden_flows_smoke.json` / `golden_flows_10k.json`。**参考答案不由被测 Go 核心生成**。
  Go 测试逐流、逐成员、逐迁移类别断言与黄金值完全相等。
- **固定流集合夹具**由 `cmd/genfixtures` 用固定种子 `20260927` 生成并提交，确定性可复现。
- 覆盖：固定映射、加/删成员的逐流迁移、与取模基线对比、纯权重变化、权重为 0、
  全部失效（三类无健康成员 code）、失效后逐流零泄漏、恢复后迁移恰为 0、
  封顶最大余数法（含 1/1/100 + cap=3 的极端可行情形与多组权重/上限不变量）、
  版本 CAS 冲突、幂等 no-op、SQLite 锁竞争→`STORE_BUSY`、坏 JSON/坏元组/路径穿越、
  **并发配置读取 + 变更（`-race`）**、HTTP 状态码与错误信封、重启恢复。
- 运行：

```bash
make test      # 全量
make race      # 带竞态检测
make cover     # 覆盖率
```

最近一次完整运行结果（含通过/失败/未执行项与环境）见 `TEST_REPORT.md`。

---

## 8. 依赖锁定

- `go.mod` 声明 `go 1.23`；`go.sum` 锁定全部模块的精确版本与哈希。
- 运行时依赖只有两个直接模块：`modernc.org/sqlite`（纯 Go SQLite，v1.34.5）与
  `gopkg.in/yaml.v3`（v3.0.1）；其余为其传递依赖，同样被 `go.sum` 锁定。
- 哈希算法固定为带版本前缀的 `FNV-1a64 + splitmix64`；并钉住了 splitmix/FNV 的已知测试向量，
  哈希一旦变更意味着全环迁移，必须显式升版本前缀。

---

## 9. 支持范围与关键取舍

**支持**
- TCP/UDP/SCTP 的端口型五元组；IPv4 与 IPv6（规范化、同族校验）。
- 整数权重、权重为 0、可选 vnode 总数上限及确定性最大余数分配。
- 成员增删改权重、立即失效、显式版本化恢复、可选 TCP 探活、持久化与重启恢复。
- 固定流集合的版本间回放、迁移分类、全量取模基线、失败落库与重放。

**刻意的取舍 / 不做的事**
- 不真正转发数据包：本服务只决定“下一跳是谁”（决策服务），不做数据面代理。
- 探活只 down 不 up；恢复是显式运维动作，以抖动安全换取不能自愈，属有意设计。
- 环查找为 O(log V)（V=vnode 总数），默认粒度 `160/权重`，偏向分布精度而非最小环。
- 持久化采用单写连接 + WAL + busy_timeout；持久化失败记录日志但不回滚内存换代
  （可用性优先于严格落盘），下一次成功变更会补齐。极严苛的“换代即落盘”语义不在范围内。
- 一致性哈希在节点数很少时，有限语料的实际份额仍可能偏离桶份额——这是概率性质，
  `/v1/stats` 明确把两份数字分开呈现而不是混为一谈。
