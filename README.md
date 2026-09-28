# 计算实例节点放置后端（Node Placement Backend）

一个从空目录构建的、可本地验证的节点放置服务：根据区域分布偏斜（zone skew）、
硬/软亲和与反亲和、资源容量、污点容忍与节点选择器，把一批待调度计算实例放置到
候选节点上；支持批量同时放置与滚动替换（surge / unavailable）。无外部依赖，
仅使用 Go 标准库 HTTP 与本地 SQLite（纯 Go 驱动 `modernc.org/sqlite`），
全部数据来自本地合成夹具。

---

## 1. 模块关系

```
cmd/placer            进程入口：加载配置/夹具、启动 HTTP 与协调循环
  ├── internal/config     独立 JSON 配置 + 环境变量覆盖
  ├── internal/version    构建版本（可 -ldflags 注入）
  ├── internal/logx       结构化 JSON 日志（run_id / version 关联）
  ├── internal/model      资源模型：节点/实例/资源/策略/放置与滚动请求结果
  ├── internal/scheduler  无状态核心：硬过滤 → 偏斜评分 → 精确穷举/贪心/滚动搜索
  ├── internal/store      SQLite 持久化适配器（清单 + run/event 审计）
  ├── internal/reconcile  协调循环：观测→调度→提交/失败重试→落库
  └── internal/server     net/http 适配器（/v1/plans, /v1/replacements, 清单…）

test/oracle   独立参考实现（朴素全枚举，不 import scheduler），仅供测试交叉验证
test/e2e      跨组件集成测试
test/testdata 合成夹具（skewed / tight）
scripts/      本地冒烟脚本（真实二进制 + curl）
```

数据流（协调循环）：

```
SQLite(store) ──snapshot──▶ reconcile.RunOnce ──▶ scheduler.Plan（无状态）
                                  ▲                      │
                          失败：attempts++/failed        │ 可行：decisions
                                  └──── 事务提交 ◀────────┘
每次运行写入 runs / events 表，并以同一 run_id 贯穿日志。
```

---

## 2. 资源模型

- **资源向量** `{milli_cpu, memory_bytes, storage_bytes}`，三维独立可比较，
  不允许负数。
- **节点 Node**：`zone/region`、容量、`labels`、`taints`（`no_schedule`
  硬 / `prefer_no_schedule` 软）、状态 `ready|disabled|not_ready`。
- **实例 Instance**：资源请求、可选 `zone` 固定、`node_selector`、
  `tolerations`、`groups`（组归属，供策略使用）、状态
  `pending|bound|evicted|failed`。
- **策略 GroupRule**：`{group, mode(hard|soft), affinity(bool), topology_key}`。
  - `hard` 是**过滤器**：违反即非法节点；
  - `soft` 只进入评分，**永远不能**让非法节点变合法。
  - topology_key 内建支持 `zone`/`region`，也支持任意节点 label。

### 关键算法语义（验收规则对应）

1. **硬约束先过滤，软分不能挽救非法节点**
   `hardCheck` 依次判定 状态 → zone → selector → 硬污点 → 三维资源 →
   组硬规则；只有全部通过的节点才进入评分。软污点/软组规则只影响排序。
2. **空域与不合格节点是否计入偏斜——显式可配**
   - `skew_domain_mode=configured`（默认）：统计**集群配置中出现的所有域**，
     含只有 disabled/不满足本批 selector 节点的域；空负载域记 0。
   - `skew_domain_mode=eligible`：只统计至少有一个节点通过
     状态/zone/selector/污点**批级预过滤**（不含资源，资源是实例相关的）的域；
     被排除的域在结果 `SkewSnapshot.excluded_domains` 中明确列出。
   - `include_empty_domains`（默认 true）控制零负载域是否纳入。
   - 每次运行 trace 里都有 `initial_skew` 快照：`loads / counted_domains /
     excluded_domains / max / min / skew`，判定依据可审计。
3. **同时放置使用临时占用，防止相互违反**
   无论精确搜索还是贪心，放置一个实例后立即 `reserve`（占用资源、加入组索引），
   后续实例看到的是“已被本批占用”的世界；回溯时 `release`。整批不可行则
   **不返回任何部分放置**。
4. **无解给出“约束冲突”而非任意放置**
   结果 `feasible=false` 时 `decisions` 必为空，`conflicts[]` 带稳定失败类别
   （`insufficient_resources / anti_affinity_conflict / zone_mismatch /
   node_not_ready / node_selector_unmatched / taint_not_tolerated /
   domain_missing / affinity_unfulfillable`），并尽量给出 `blocked_by`。
   输入非法与“搜索预算耗尽”是 **error**（4xx/422），不会被伪装成冲突或成功。

### 求解器

- **精确穷举（小集合，默认 pending ≤ 12）**：MRV（最少可行节点优先）回溯 +
  按评分排序分支；枚举**所有**可行完整分配，按目标字典序选全局最优，
  并以 (instance→node) 字典序打破平局，保证结果确定性。访问数受
  `search_budget` 保护，耗尽返回显式 `SearchExhausted` 错误（非冲突、非成功）。
- **贪心（大集合）**：MRV + 临时占用，逐步 `reserve`，每步记录所有候选节点的
  分数与偏斜快照（trace 可核验“为什么选它”）。
- **目标元组（越小越优）**：`(域偏斜 max-min, 域负载平方和, 软规则违反数)`。
  平方和用于在偏斜相同时偏好更均匀的分布；最终再用 node_id 字典序确定性破平。

### 滚动替换

`Replace` 搜索交错的 `place_new / evict_old` 序列，并在**每个中间状态**都满足：

- 不超过 `max_surge`（旧实例仍在时新实例已起的数量）；
- 不超过 `max_unavailable`（新实例未起旧实例已被驱逐的数量）；
- 任意时刻资源与硬组规则都合法（旧世代**计入**占用，因此没有空闲域时
  surge 滚动需要先 evict，这正是真实语义）。

最终放置用同一目标优化；同等最终放置下优先“尽量晚驱逐”的序列。
`max_surge=0` 且 `max_unavailable=0` 直接判为输入非法（无第一步可走）。

---

## 3. 本地验证

需要 Go 1.23+（开发环境 go1.23.4），无需 CGO、无需联网账号。

```bash
# 一键：gofmt 检查 + vet + 全部测试
make check

# 仅测试（单元 + 独立 oracle 差分模糊 + 存储/协调/HTTP + e2e）
go test ./...
go test -v ./...          # 查看每个用例
go test -race ./...       # 竞态检测

# 真实二进制 + curl 的端到端冒烟（9 项断言，输出 PASS/FAIL）
make smoke

# 用夹具跑单次协调（不启动常驻服务）
go run ./cmd/placer --fixture test/testdata/skewed.json \
  --db /tmp/demo.db --once
```

### 预期判断方式

- `go test ./...`：每个包打印 `ok`，退出码 0。核心包用例见
  `internal/scheduler/*_test.go`（偏斜选空域、域计数两模式、硬先于软、
  域缺失、相互反亲和、资源不足、同时占用、滚动 surge/unavailable、
  预算耗尽为 error、贪心 trace 可审计、与独立 oracle 的穷举一致性 + 400 组随机差分）。
- `make smoke`：结尾打印 `ALL SMOKE CHECKS PASSED (9)`，退出码 0；
  覆盖 health/version、可行分散（200）、无解（HTTP **409** + 类别）、
  相互反亲和（409 + anti_affinity）、滚动首步 surge 到空闲域、坏请求（**400**）。
- 日志每行是 JSON，含 `ts/level/event/version/component/run_id`，
  失败为 `"level":"error"`，不会把异常折叠成成功。

### 手工 API 示例

```bash
go run ./cmd/placer --addr :18080 --db /tmp/demo.db &

# 可行放置（硬 zone 反亲和 → 两个实例分散到不同 zone）
curl -s -X POST localhost:18080/v1/plans -H 'Content-Type: application/json' -d '{
  "nodes":[{"id":"a1","zone":"za","status":"ready","capacity":{"milli_cpu":4000,"memory_bytes":1e10,"storage_bytes":1e11}},
           {"id":"b1","zone":"zb","status":"ready","capacity":{"milli_cpu":4000,"memory_bytes":1e10,"storage_bytes":1e11}}],
  "instances":[{"id":"i1","state":"pending","request":{"milli_cpu":500,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}},
               {"id":"i2","state":"pending","request":{"milli_cpu":500,"memory_bytes":1,"storage_bytes":1},"groups":{"app":"w"}}],
  "policy":{"groups":[{"group":"app","mode":"hard","affinity":false,"topology_key":"zone"}]}}'

# 无解 → HTTP 409 + conflicts[].code
# 滚动替换
POST /v1/replacements
# 清单与协调
GET  /v1/nodes | PUT /v1/nodes/{id} | POST /v1/nodes/{id}/status
GET  /v1/instances | PUT/DELETE /v1/instances/{id}
GET/PUT /v1/policy
POST /v1/reconcile
GET  /v1/runs/{run_id} | GET /v1/runs/{run_id}/events
```

可在请求头传 `X-Run-Id: <id>` 强制关联 ID；响应与日志、runs/events 表共用它。

---

## 4. 算法假设

- **域负载**以“实例个数”计量（非资源量）。资源容量仍按三维向量严格约束，
  偏斜优化的目标是实例数量的均匀分布。
- 组规则按 `Instance.Groups[group]` 的取值划分组员；同值才构成同组。
- **硬亲和**要求同组实例位于同一域（已存在成员的域）；硬反亲和要求不同域。
  软版本仅计分。一个 group+topology 上可同时存在不同形态的规则
  （affinity 与 anti-affinity、hard 与 soft），完全相同的重复规则被判为非法。
- 节点无 topology_key 对应域时，对参与该硬组规则的实例判 `domain_missing`，
  不会被静默计入或误判为资源问题。
- 评分只在**通过全部硬过滤**的候选间进行；目标 `(skew, sum_squares,
  soft_violations)`，最后按 node_id 字典序确定性破平。
- 滚动替换中旧世代与新世代 id 空间必须不相交（约定新世代用 `-v2` 之类后缀），
  用 `replaces` 映射配对。
- 持久层用单写互斥 + 事务保证“整批绑定”原子性；若某个实例在快照后已不再
  pending（乐观并发），整批提交回滚。
- 协调循环对冲突做有限重试（`max_retries`），耗尽后置 `failed` 并保留
  `attempts/last_code`；基础设施错误（DB、序列化、搜索预算）作为 error 上抛，
  不计入“成功放置”。

---

## 5. 依赖版本

| 依赖 | 版本 | 说明 |
|---|---|---|
| Go | 1.23.4（要求 1.23+） | 标准库 `net/http`、无 Web 框架 |
| modernc.org/sqlite | v1.34.5 | 纯 Go（无 CGO）SQLite 驱动 |
| 其传递依赖 | 见 `go.mod` / `go.sum` | libc/mathutil/memory 等，均为现代c |

构建版本可注入：

```bash
go build -ldflags "-X placer/internal/version.Version=v1.0.0 \
  -X placer/internal/version.Commit=$(git rev-parse --short HEAD) \
  -X placer/internal/version.BuildTime=$(date -u +%FT%TZ)" \
  -o bin/placer ./cmd/placer
```

---

## 6. 测试如何避免“自证正确”

- 关键穷举结果与 `test/oracle` 中**完全独立的第二实现**（朴素笛卡尔积枚举、
  从零重算可行性与目标，不共享 scheduler 任何代码/数据结构）逐一比对：
  可行性、最优分配、目标元组、内在失败类别。
- `TestFuzz_DifferentialAgainstOracle` 用 400 个确定性随机种子生成小集群做
  差分测试（seed 即输入身份，失败可复现）。
- 其余测试断言**具体放置结果与失败类别**（具体节点、具体 RejectCode、
  blocked_by 对端、HTTP 状态码），而非仅“接口可调用”。
- 滚动序列由测试侧独立 `replayEnvelope` 逐步重放，独立核对 surge/unavailable
  计数、资源超分与硬反亲和，而非信任被测核心自身的记录。
