# fieldapply — 声明式字段级合并后端

类似 Kubernetes Server-Side Apply 的字段级三方合并服务：多个**管理者（field manager）**各自声明自己关心的字段，服务端按字段所有权合并；默认不抢占，冲突返回字段路径与当前管理者；可用 `force` 显式强制接管。值与所有权在同一事务落库，每次应用生成一个可审计的历史版本和可重放的诊断记录。

技术栈：Go 1.23 + 标准库 `net/http` + SQLite（`modernc.org/sqlite`，纯 Go，无 CGO）。

---

## 1. 合并语义

### 1.1 字段路径与三种列表

所有权以**叶子路径**记录，渲染形式稳定、可排序：

| 形式 | 示例 |
| --- | --- |
| 对象字段 | `.spec.replicas` |
| 按键映射列表元素的字段 | `.spec.containers[name="web"].port` |
| 集合列表元素 | `.spec.tags[_="z"]` |
| 原子列表（整组一个叶子） | `.command` |

在资源创建时用 `schema` 声明每个数组路径的语义：

- **`atomic`（默认）**：整个数组是一个叶子。有效载荷不同且有别的管理者时，在该数组路径上整体冲突；`force` 整体替换。
- **`set`**：标量元素的集合，按值判等。每个元素是独立叶子（`.tags[_="z"]`），两管理者可各自增删元素、共享相同元素。
- **`keyed`**：对象元素按标量键字段（默认 `name`，可在 `schema.keys` 改）合并；相同键的元素逐字段合并，键字段本身也是一个受所有权约束的叶子。

### 1.2 未提交字段 ≠ 显式删除

- **未提交（uncommitted）**：活值（live）里存在、但没有任何管理者的 last-applied 覆盖它的字段（例如绕过 API 的手工直写）。任何管理者省略它都不会删除它。
- **显式删除**：管理者在新配置中**省略**（或发送 JSON `null`，null 会在合并前按"放弃意见"裁剪）一个它拥有的字段——它释放该叶子的份额；若它是唯一持有者，字段被删除并记入 `changes.removed`；若仍有其他共享者，值保留。

### 1.3 冲突、原管理者与不抢占

应用一个值时，逐叶子应用规则：

1. 期望值 == 活值：直接**共享所有权**（不报冲突、不改值）。
2. 期望值不同且叶子被**其他**管理者持有：默认返回 `409 state_conflict/field_conflict`，冲突体列出每个阻塞路径、其当前管理者、当前值与期望值；**活值保持不变，本次应用全有或全无**。
3. 期望值不同但叶子无人持有：直接采纳（adopt）。
4. `force=true`：只对"会冲突"的差异叶子剥离其他管理者的份额并接管；值相同的既有共享者保留；force 范围严格限定在涉及的叶子，无关字段不受影响。
5. 结构冲突（对象 ↔ 数组）：按被替换子树的每个叶子分别走上面的冲突/接管规则。

可选乐观并发：请求带 `baseRevision`，与当前 revision 不符时返回 `revision_stale`，不进入合并。

### 1.4 事务与历史

协调循环对**同一资源串行**执行 `读取快照 → 纯函数合并 → 提交`。提交在单个数据库事务中同时更新：活值、所有权表、该管理者的 last-applied、历史版本。历史条目含 run id、管理者、原因、是否强制、按叶子的 added/changed/removed 差异。

纯所有权变化（如以同值共享叶子）会推进 revision 以便审计，但 `changes` 中不出现值差异。

---

## 2. 工程结构

```
cmd/fieldapply/          入口：flag/配置、SQLite/内存存储、HTTP 服务、优雅退出
internal/model/          数据与错误契约：路径、字段集合、所有者、列表 schema、分类错误
internal/merge/          纯合并引擎（无 I/O）+ schema 感知 Diff
internal/store/          持久化契约 Store；内存适配器与 SQLite 适配器（事务、错误映射）
internal/coord/          协调循环：按资源串行、有界队列、诊断日志、乐观版本
internal/httpapi/        标准库 HTTP 适配器、统一错误信封与状态码
internal/diag/           JSON-lines 诊断日志（run id、阶段、关键中间状态）
examples/                冒烟脚本与示例请求
```

模块间只通过显式契约通信：`merge.Inputs/Result`（纯）、`store.Store/Snapshot/Commit/HistoryEntry`、`coord.ApplyRequest/ApplyOutcome`、`model.Error{Category,Code,Conflicts}`。

错误分类（HTTP 状态码）：

| Category | 含义 | 状态码 |
| --- | --- | --- |
| `invalid_input` | JSON/schema/键字段错误、非对象配置 | 400 |
| `state_conflict` | 字段冲突、revision 过期、重复创建 | 409 |
| `not_found` | 资源不存在 | 404 |
| `resource_exhausted` | 队列饱和（503）、SQLite 磁盘/锁 | 503 |
| `computation_failure` | 存储异常、状态损坏、内部 panic 兜底 | 500 |

---

## 3. 本地启动

需要 Go 1.23+。依赖已锁定在 `go.mod` / `go.sum`（纯 Go SQLite，无需 C 工具链）。

```bash
go build -o bin/fieldapply ./cmd/fieldapply

# SQLite 持久化（默认）
./bin/fieldapply -addr 127.0.0.1:8080 -db fieldapply.db -journal fieldapply.journal.jsonl

# 或一次性内存模式
./bin/fieldapply -db :memory: -journal -
```

也可用环境变量 `ADDR`、`DB`、`JOURNAL`（`-` 输出到 stdout，留空关闭）。

运行全部测试（含差分测试与 `-race`）：

```bash
go test ./...
go test -race ./...
```

一键冒烟（先启动服务）：

```bash
BASE=http://localhost:8080 bash examples/smoke.sh
```

---

## 4. HTTP 示例

### 创建资源（声明集合/按键列表）

```bash
curl -s -X POST localhost:8080/v1/resources -d '{
  "id":"app",
  "manager":"platform",
  "body":{"spec":{"replicas":3,"tags":["x","y"],
    "containers":[{"name":"web","image":"web:1","port":8080}]}},
  "schema":{"lists":{".spec.tags":"set",".spec.containers":"keyed"},
            "keys":{".spec.containers":"name"}}
}'
```

### 应用（两个管理者改不同字段）

```bash
# autoscaler 先以当前值 3 共享，再强制改为 7
curl -s -X POST localhost:8080/v1/resources/app/apply \
  -d '{"manager":"autoscaler","body":{"spec":{"replicas":3}}}'
curl -s -X POST localhost:8080/v1/resources/app/apply \
  -d '{"manager":"autoscaler","force":true,"reason":"scale-up",
       "body":{"spec":{"replicas":7}}}'
```

### 默认不抢占：409 带原管理者

```bash
curl -s -X POST localhost:8080/v1/resources/app/apply \
  -d '{"manager":"platform","body":{"spec":{"replicas":11}}}'
# 409
# {"error":{"category":"state_conflict","code":"field_conflict",
#   "conflicts":[{"path":".spec.replicas","owners":["autoscaler"],
#                 "current":7,"applied":11}]}}
```

### 嵌套按键列表 / 集合 / 删除 / 接管

```bash
# netpol 增加集合元素 z（独立叶子，不碰 x/y）
curl -s -X POST localhost:8080/v1/resources/app/apply \
  -d '{"manager":"netpol","body":{"spec":{"tags":["z"]}}}'

# 强制接管 web 容器的 port（image 等其它叶子仍归原管理者）
curl -s -X POST localhost:8080/v1/resources/app/apply \
  -d '{"manager":"netpol","force":true,
       "body":{"spec":{"containers":[{"name":"web","port":9090}]}}}'

# platform 省略 worker 容器即显式删除；配置中携带他人字段的当前值以免误报冲突
curl -s -X POST localhost:8080/v1/resources/app/apply -d '{
  "manager":"platform",
  "body":{"spec":{"replicas":7,"tags":["x","y"],
    "containers":[{"name":"web","image":"web:1","port":9090}]}}}'
```

### 查询与审计

```bash
curl -s localhost:8080/v1/resources/app            # 活值 + revision
curl -s localhost:8080/v1/resources/app/owners     # 每个叶子的管理者
curl -s 'localhost:8080/v1/resources/app/history?limit=20'
```

更多请求见 `examples/requests.http`。

### 诊断日志

每次应用写一条或多条单行 JSON 到 journal：`received → conflict | merged/committed → error`，带：

- `run_id`：时间前缀 + 随机后缀（如 `r-20260928T080105Z-10bfce1d01875e31`），可用于重放/检索；
- 冲突阶段的 `detail`：完整冲突路径、owners、current/applied，即判断理由；
- 提交阶段的 `summary/detail`：added/changed/removed 计数与新接管路径。

---

## 5. 测试策略

- `internal/merge/merge_test.go`：具体结果断言——双管理者改不同字段且无关字段不丢、冲突路径与原管理者、force 只接管目标叶子、嵌套 keyed 列表、set/atomic 列表、显式删除 vs 未提交、同值共享、结构冲突、null 语义、非法输入分类、revision 过期。
- `internal/merge/reference_diff_test.go`：**独立参考实现**（扁平叶子表 + 集合运算，与生产引擎不共享任何合并代码）。随机生成 400 个序列 × 12 步交错应用（含 20% force），逐状态比对两边的活值表、所有权表与冲突判定。
- `internal/store/store_test.go`：同一套契约测试同时跑内存与 SQLite 适配器（创建、重复、not-found、乐观版本、原子提交、历史、每管理者 last-applied）。
- `internal/coord/coord_test.go`：端到端多管理者场景与 journal 内容断言；队列饱和返回 `resource_exhausted/queue_full`；损坏状态归为 `computation_failure` 且不 panic。
- `internal/httpapi/server_test.go`：HTTP 状态码与统一错误信封（400/404/409/503），真实接口下的冲突、接管、删除、历史。

实际执行结果见 `TEST_RESULTS.md`（含曾出现并修复的失败项）。

---

## 6. 支持范围与关键取舍

**支持**

- 任意深度 JSON 对象；`atomic` / `set` / `keyed` 三种数组语义；多管理者、同值共享、按叶子 force、乐观 revision。
- 未提交字段保留；显式删除与共享者释放；结构冲突的逐叶子处理。
- 值 + 所有权 + last-applied + 历史同事务（SQLite WAL）；按资源串行的协调循环；有界背压；可重放诊断。

**明确的取舍 / 限制**

1. **null 语义**：对象成员显式 `null` 在合并前按"放弃意见"裁剪（等价省略，释放份额）；数组中的 `null` 标量元素保留。要把字段设为 JSON `null` 值不在支持范围内（与 K8s SSA 一致）。
2. **数字比较按数值**（`1` 与 `1.0` 视为相等，内部用 `big.Rat`），但存储保留 JSON 原文形式。
3. **keyed 列表只支持标量键**（字符串/数字/布尔），默认键名 `name`；重复键、非对象元素在请求侧拒绝。
4. **schema 随资源固定**（创建时声明），不支持在线改 schema；schema 路径需指向数组，否则创建即拒绝。
5. **合并作用于叶子**：空对象 `{}` 不拥有任何叶子；原子数组的内部结构不产生所有权。
6. **并发粒度是资源**：不同资源并行、同资源排队；SQLite 写入连接数设为 1，靠 `busy_timeout` 与 WAL 应对锁竞争。这是本地/单实例取舍，不做跨实例分布式锁。
7. 认证/鉴权与多租户不在范围内：`manager` 是请求自报的标识（本地合成环境）。
