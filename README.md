# admission — 本地资源准入流水线

一个完全本地运行的**资源请求准入（admission）流水线**参考实现：请求先经过**有序变换器
（mutator）链**修改声明路径，链收敛后再执行**最终校验器（validator）**，通过后在单个
SQLite 事务内提交资源、配额与审计。

- 语言/技术栈：**Go 1.23 + 标准库 `net/http` + SQLite**（`github.com/mattn/go-sqlite3`，
  仅一个第三方依赖，随模块缓存离线可用；需要 CGO/gcc）。
- 无任何生产账号或真实业务数据：外部参与者只有一个**本地配额账本**，失败场景用确定性
  **脚本夹具（scripted fixture）**制造。
- 所有数据落本地 SQLite 文件；每次准入写一条带运行编号的 JSONL 运行日志用于问题重放。

## 它保证什么

| 需求 | 实现位置 |
| --- | --- |
| 变换器**只修改声明路径** | `internal/patch`：插件返回 PatchOp，框架按 `DeclaredPaths()` 校验后才应用；`/apiVersion`、`/kind`、`/metadata/name` 等受保护路径永远不可写 |
| 链后**必须再最终校验** | `internal/pipeline`：变换器迭代到不动点后，校验器按序执行且只执行一次 |
| 重入变换**幂等** | 每个内置变换器观察到目标态即不产出补丁；重复提交同一 UID 重放已存裁决，最终摘要 digest 相同，配额只占一次 |
| **禁止无限循环** | 变换器链有固定 `maxPasses` 收敛上限；不收敛返回明确的 compute 失败（测试用振荡插件证明恰好只跑上限次） |
| 失败开放/关闭**显式配置** | 启动配置中每个插件必须写 `failPolicy: open|closed`，无默认值；校验配置时强制 |
| **超时原因可区分** | 每插件独立超时；`timeout`(504)、`invalid_input`(400)、`state_conflict`(409)、`resource_exhausted`(429)、`validation_denied`(422)、`compute_failure`(500) 分类互斥 |
| **禁止部分变换提交** | 补丁应用到深拷贝上；任一补丁非法即丢弃整个拷贝，工作对象保持原样，提交为单事务回滚 |
| 请求与审计**绑定最终对象摘要** | 响应、`requests.final_digest`、`audits` 都带最终对象的 sha256 摘要；被拒绝时也绑定“被拒对象”的摘要 |

## 目录结构

```
cmd/admissiond/            程序入口（HTTP 服务 + 后台协调循环）
internal/
  model/      资源模型、请求/响应/步骤/摘要的数据契约与失败类别 Reason
  patch/      JSON-Pointer 补丁子集 + 声明路径守卫 + 深拷贝
  plugins/    插件接口、失败策略、内置变换器/校验器、脚本夹具、配额端口
  pipeline/   准入核心：有序变换器不动点迭代 + 链后最终校验
  storage/    SQLite：请求账本、资源、审计、配额账本（含提交时权威容量门）
  coordinator/编排：同步准入路径 + 崩溃恢复协调循环、重入幂等
  adapter/    标准库 HTTP 适配层（JSON、状态码/类别映射、只读查询端点）
  config/     启动配置与本地默认值目录
  runlog/     JSONL 运行日志（运行编号 + 每步补丁 + 判定理由）
  app/        组合根（按配置把具体实现装配起来）
configs/     启动配置 admissiond.json + 默认值 defaults.json
samples/     样例请求
tests/integration/ 端到端测试（真实 HTTP + 真实 SQLite 文件 + 重启 + 崩溃恢复）
```

## 快速开始

前置：Go 1.23+、gcc（CGO，`go-sqlite3` 需要）。

```bash
cd opp286/b
go build ./...          # 离线即可，依赖已在模块缓存
go test ./...           # 运行全部单元 + 集成测试
```

启动服务（默认 127.0.0.1:8080，数据在 ./data）：

```bash
go run ./cmd/admissiond -config configs/admissiond.json
```

提交一个**最小请求**（spec 为空，默认值由变换器补齐）：

```bash
curl -s -X POST http://127.0.0.1:8080/v1/requests \
  -H 'Content-Type: application/json' \
  -d @samples/request-create-minimal.json
```

返回（节选）展示了**有序补丁链**与**插件相互影响**：默认值补出 `replicas=1`，
容量插件据此算出 `capacity=100`，UID 被盖到注解，最后通过校验，并返回最终摘要：

```json
{
  "uid": "demo-create-0001",
  "decision": "allowed",
  "finalObject": { "kind": "Widget", "metadata": { "annotations": { "admission.uid": "demo-create-0001" }, "name": "orders", "namespace": "shop" },
    "spec": { "capacity": 100, "replicas": 1, "schedule": "always" } },
  "finalSummary": { "kind": "Widget", "name": "orders", "namespace": "shop", "replicas": 1,
    "digest": "588f278e8a14471c7afcc205899f8b5653aa057f34fd4c24969ddb126340a65a" },
  "steps": [
    { "order": 1, "phase": "mutate", "plugin": "defaults", "patches": [
        {"op":"add","path":"/spec/replicas","value":1},
        {"op":"add","path":"/spec/schedule","value":"always"} ], "decision":"applied" },
    { "order": 2, "phase": "mutate", "plugin": "capacity", "patches": [
        {"op":"add","path":"/spec/capacity","value":100} ], "decision":"applied" },
    { "order": 3, "phase": "mutate", "plugin": "stamp-uid", "patches": [
        {"op":"add","path":"/metadata/annotations/admission.uid","value":"demo-create-0001"} ], "decision":"applied" },
    { "order": 4, "phase": "mutate", "plugin": "defaults", "patches": [], "decision":"applied" },
    { "order": 5, "phase": "mutate", "plugin": "capacity", "patches": [], "decision":"applied" },
    { "order": 6, "phase": "mutate", "plugin": "stamp-uid", "patches": [], "decision":"applied" },
    { "order": 7, "phase": "validate", "plugin": "schema", "decision":"applied" },
    { "order": 8, "phase": "validate", "plugin": "quota", "decision":"applied" }
  ]
}
```

注意第 4–6 步补丁为空：第二轮迭代全部插件都观察到目标态，**链已收敛**（幂等）。

## 失败类别（互斥、可区分）

| 类别 Reason | HTTP | 含义 |
| --- | --- | --- |
| `invalid_input` | 400 | 输入错误：缺 uid/对象/名称、JSON 损坏 |
| `validation_denied` | 422 | 策略拒绝（插件正确判否）：如 replicas 超对象上限 |
| `state_conflict` | 409 | 状态冲突：重复 UID 处理中、CREATE 名称已存在、UPDATE/DELETE 目标不存在 |
| `resource_exhausted` | 429 | 资源耗尽：超过每类配额 |
| `timeout` | 504 | 插件计算超时（与下面的计算失败明确区分） |
| `illegal_path` | 500 | 变换器试图写未声明/受保护路径（**即使 fail-open 也必拒**） |
| `compute_failure` | 500 | 计算失败：插件返回错误、panic、链不收敛 |

示例（默认配置 Widget 配额=7）：

```bash
# 422 策略拒绝（replicas 99 > 单对象上限 5），且响应仍带被拒对象摘要
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8080/v1/requests \
  -H 'Content-Type: application/json' -d @samples/request-create-toomany.json   # -> 422

# 400 输入错误
curl -s -X POST localhost:8080/v1/requests -d '{"operation":"CREATE"}'
# {"error":"uid is required","reason":"invalid_input","category":"input_error"}

# 429 配额耗尽（响应 decision=denied, reason=resource_exhausted）
```

被**允许**返回 200；被**策略拒绝**是一次完成的准入裁决，但状态码反映失败类别，
调用方无需解析响应体即可区分类别。

## 重入、协调循环与配额记账

- **重复提交同一 UID**：直接重放已存终态裁决，审计不新增 run、配额不重复占用。
- **崩溃恢复**：请求先落 `pending`，处理时带租约置 `processing`；后台协调循环
  （`reconcileIntervalMs`）认领 pending 与租约过期的 processing 行重跑。
  集成测试 `TestReconcile_RecoversPendingRow` 与 `TestRestart_ReplaysVerdictFromSQLite` 覆盖。
- **配额精确**：校验期只放“在途 hold”（CREATE 全量、UPDATE 仅正增量、DELETE 无）；
  权威容量门在**提交事务内**对真实 `resources.replicas` 求和 + 他人在途 hold，
  因此 UPDATE 不会重复计入旧 CREATE、DELETE 立即释放、删除后可按满额重建。

## 审计与可重放运行日志

```bash
curl -s localhost:8080/v1/requests/demo-create-0001/audits   # 含 runId、每步补丁、最终摘要
cat data/runs/admission-runs.jsonl                           # 每次准入一行
```

每行包含可 grep 的运行编号（如 `run-20260928T081613Z-000001`）、请求、裁决、类别、
**每一步的中间补丁与判定理由**、最终摘要——足以据此复现问题：

```bash
grep '"runId":"run-20260928T081613Z-000001"' data/runs/admission-runs.jsonl
```

## 配置（configs/admissiond.json）

每个插件的 `failPolicy`（open/closed）与 `timeoutMs` 都必须显式给出，缺失即启动失败：

```json
"mutators": [
  { "type": "defaults",  "failPolicy": "closed", "timeoutMs": 200 },
  { "type": "capacity",  "failPolicy": "closed", "timeoutMs": 200, "perReplica": 100 },
  { "type": "stamp-uid", "failPolicy": "closed", "timeoutMs": 200 }
],
"validators": [
  { "type": "schema", "failPolicy": "closed", "timeoutMs": 200, "maxReplicas": 5 },
  { "type": "quota",  "failPolicy": "closed", "timeoutMs": 500 }
]
```

## 测试

```bash
go test ./...                       # 全部
go test -race -count=1 ./...        # 竞态检测 + 不用缓存
go test -v ./internal/pipeline/     # 观察每个失败类别的用例名
```

测试刻意断言**具体结果与失败类别**，而非“接口能调用”：默认值补齐结果、每步补丁顺序、
越权路径必拒、超时 open/closed 差异、振荡插件的收敛上限次数、重复调用 digest 一致、
配额 UPDATE/DELETE 精确记账、重启重放等。失败夹具（`internal/plugins/scripted*.go`）
与被测核心**相互独立**，不是由被测核心自己生成参考答案。

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/CONTRACTS.md](docs/CONTRACTS.md)。
