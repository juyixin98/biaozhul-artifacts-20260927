# fieldmerge — 字段级声明式 apply 合并后端

一个类似 Kubernetes Server-Side Apply 的字段级合并后端：多个"管理者"
（manager）可以对同一资源各自声明自己关心的字段，后端按字段所有权做三方
合并，冲突时返回字段路径与原管理者，默认不抢占；显式 `force` 才能接管。

所有外部依赖都是本地的：标准库 HTTP + SQLite（纯 Go 驱动，**无 cgo**），
下游执行器是一个把状态写成本地 JSON 文件的适配器，外加可注入故障的适配器。

## 它做什么

- **三种列表语义（schema 声明）**
  - `atomic`：整个列表是一个字段，最后写入者所有，整体替换。
  - `set`：标量集合，按值去重、按值记所有权；多个管理者可"共享"同一个值。
  - `map`：对象列表按 `key` 字段标识（对应 k8s `listType=map`），所有权精确
    到元素内的每个叶子字段。
- **未提交 ≠ 显式删除**：config 中缺席只回退该管理者自己拥有的字段；显式
  JSON `null` 才是删除；删除他人字段需要 force。
- **冲突不抢占**：返回所有冲突的字段路径、原管理者、原因；整次 apply 事务回滚，
  live / 所有权 / revision 都不变，也不写历史。
- **值与所有权同事务**：合并后的 live、所有权表、历史版本在同一个 SQLite
  事务提交；历史带每字段变更的前/后值、管理者、force 标记和 run id，可审计重放。
- **协调循环**：apply 成功后异步入队，worker 调适配器下发，支持指数退避重试、
  永久失败与重试预算耗尽的区分、有界队列（满了返回明确的资源耗尽错误）、
  到期轮询兜底。
- **错误契约**：输入错误 / 状态冲突 / 资源耗尽 / 计算失败 / 内部存储故障
  全程带分类（`internal/apperr`），HTTP 映射到不同状态码。
- **可重放诊断**：每个请求有 `X-Run-Id`（可用同名 header 串联重试链）；
  服务输出 JSONL 结构化日志（含 run id、关键中间状态、判断原因）；测试同样
  把运行编号、中间状态、断言理由写入 `testlogs/*.jsonl`。

## 目录结构

```
cmd/fieldmerged/        服务入口（HTTP + SQLite + 协调循环）
internal/
  apperr/               跨模块错误分类契约
  fieldpath/            字段路径记法 .f / [k="v"] / [^"v"] 与解析、查找
  schema/               列表语义声明（atomic/set/map）
  merge/                纯函数三方合并引擎（不碰存储/时钟/IO）
  store/                SQLite：资源、所有权、历史、schema 四张表（同事务）
  adapter/              下游执行器：文件适配器 + 故障注入/不可用适配器
  reconcile/            协调循环：有界队列、worker、退避重试、到期轮询
  server/               HTTP 边界：校验、状态码映射、run id、panic 兜底
  log/                  JSONL 结构化日志
  testkit/              测试运行记录器（run id + 中间状态 + 断言理由）
testdata/threeway/      三个场景的手工三方合并推导（独立参考答案）
examples/API.md         完整 API 示例
scripts/demo.sh         一键本地演示
vendor/                 锁定的依赖（纯 Go SQLite，可离线构建）
```

模块边界与契约：`merge` 是纯引擎，输入 `Input+[]Claim`、输出 `Result`；
`store` 负责在单事务内驱动 merge 并落 live/ownership/history；`reconcile`
只依赖 store 的查询/状态更新接口和 `adapter.Adapter` 接口；`server` 只做
协议适配，所有错误经 `apperr.As` 分类。

## 快速开始

要求 Go 1.23+（依赖已 vendor，不需要 cgo、不需要联网）。

```bash
go run ./cmd/fieldmerged -addr :8080 -data ./data
# 另一个终端：
./scripts/demo.sh
```

或先构建：

```bash
make build        # -> bin/fieldmerged
make run          # 前台启动
make test         # 全量测试（含 -race）
make demo         # 需要服务已在 :8080 运行
```

启动后：
- HTTP：`http://localhost:8080`
- SQLite：`data/fieldmerge.db`
- 下游适配器产物：`data/applied/<kind>/<name>.json`
- 结构化日志：`data/logs/fieldmerged.jsonl`

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| PUT | `/v1/schemas/{kind}` | 声明列表语义 |
| GET | `/v1/schemas/{kind}` | 读取声明 |
| POST | `/v1/{kind}/{name}/apply` | 字段级 apply（`manager`/`force`/`config`） |
| GET | `/v1/{kind}/{name}` | 当前 live 与协调状态 |
| GET | `/v1/{kind}/{name}/ownership` | 字段所有权矩阵 |
| GET | `/v1/{kind}/{name}/history` | 历史版本（审计/重放） |
| GET | `/v1/resources?kind=` | 资源列表 |

完整请求/响应示例见 [`examples/API.md`](examples/API.md)，或直接运行
`scripts/demo.sh`。

## 字段路径记法

```
spec.replicas                       对象字段
ingresses[name="edge-1"].host       map 列表元素（键值用 JSON 标量记法）
tags[^"canary"]                     set 元素（^ 表示按值）
ports[^8080]  flags[^true]          数字/布尔标量同理
```

map 列表的身份键本身（如 `name`）编码在路径里，不单独占所有权、不可被
接管或单独删除。

## 测试策略

- **手工三方合并对照**：`testdata/threeway/scenario_{A,B,C}.md` 是用纸笔推导
  的期望值；`internal/merge/golden_{a,b,c}_test.go` 把这些**手写**的 live 与
  所有权矩阵逐字段断言。参考答案不由被测核心生成。
  - A：两个管理者改不同字段 → 非强制冲突（带原管理者）→ force 接管 →
    无关字段全程不丢。
  - B：嵌套 map/set/atomic 列表、嵌套叶子冲突、force 只改目标叶子、
    省略他人 set 值不删除、atomic 整表接管。
  - C：缺席自己字段（回退）/ 缺席他人字段（静默保留）/ 显式 null 自己字段
    （删除）/ 显式 null 他人字段（冲突）。
- **独立测试断言具体结果与失败类别**，不是"接口能调通"：
  - `store`：冲突零落盘（live/revision/history 都不变）、值与所有权同事务、
    历史带 from/to、资源/负载耗尽分类、损坏 live→compute_failure、
    损坏所有权→internal。
  - `reconcile`：注入前 2 次失败后恢复（断言调用次数与落地产物）、永久失败
    不重试且标记 fatal、可重试失败耗尽预算后 attempts 精确等于上限、
    队列满→resource_exhausted/queue_full。
  - `server`：409 响应体含路径与 owners、各类错误的状态码、413、
    X-Run-Id 生成与客户端透传、冲突不产生历史行、协调最终 synced。
- **测试日志**：`internal/testkit` 把每个用例的 run id、apply 输入、引擎实际
  中间状态（冲突/变更/剪枝）、每条断言的通过与否和理由写到 `testlogs/`，
  出问题时可用 run id 重放。

```bash
go test ./... -race
cat internal/merge/../../testlogs/*.jsonl   # 测试运行记录
```

## 支持范围与关键取舍

1. **全量声明（SSA 风格）**：每次 apply 的 config 是该管理者的完整声明。
   缺席的含义是"我不再要这个字段"（仅对自己拥有的生效），而不是补丁式
   "忽略"。这让"未提交"与"显式删除"可以清晰区分，也避免了部分提交导致的
   所有权歧义。
2. **冲突粒度到叶子，但接管按子树**：冲突报告精确到字段路径（含嵌套 map
   元素叶子和 set 值）；`force` 会接管该路径及其下所有声明。冲突时整次
   apply 不落盘（不做部分提交），语义最简单、最好审计。
3. **set 语义**：无序、去重、按值共有；输出时声明值在前、因冲突或遗漏而
   保留的他人值在后。省略一个不属于自己的值不会删除它。
4. **map 元素只剩身份键时会被剪除**，不产生 `{"name":"x"}` 空壳；身份键
   不参与所有权。
5. **无显式版本号乐观锁**：写入由进程内互斥 + SQLite 单写事务串行化，
   每次成功 apply 递增 revision 并完整记录历史；未做基于 revision 的
   条件写（多副本部署时需要再加）。
6. **适配器是本地抽象**：文件适配器用 tmp+rename 原子落盘，只代表"下游
   成功收敛"这一契约；真实 K8s/云客户端实现 `adapter.Adapter` 接口即可接入。
7. **规模与限制**：默认资源数 10k、请求体/live 1MiB（store）、HTTP body
   2MiB、协调队列 128，超限返回 resource_exhausted 而非 OOM/静默丢弃。
8. **已知边界**：不支持数字/字符串等标量按 JSON patch 形式的部分更新；
   未声明的列表出现在 config 中会被直接拒绝（防止把本应原子/合并的列表
   误当成普通叶子处理）。
