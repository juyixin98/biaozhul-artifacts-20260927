# 模块间数据与错误契约

本文档固定跨模块边界传递的数据结构与错误语义。代码位置以包路径标注。

## 1. 模型契约（`internal/model`）

### Request（入站）
```jsonc
{
  "uid": "string, 必填",
  "operation": "CREATE | UPDATE | DELETE",
  "object":    { ... },          // CREATE/UPDATE 必填的信封对象
  "oldObject": { ... }           // UPDATE 的旧对象（配额增量需要）；DELETE 身份
}
```
信封对象要求 `apiVersion`、`kind`、`metadata.name`。`Request.Validate()` 在进入流水线前
同步执行，任何问题一律是 `invalid_input`（适配器 400），不产生审计 run。

### Response（出站）
```jsonc
{
  "uid": "...",
  "decision": "allowed | denied",
  "reason": "Reason 或省略（allowed）",
  "message": "...",
  "finalObject":  { ... },       // allowed：最终对象；denied：链后被拒对象（若有）
  "finalSummary": { "kind","name","namespace","replicas","digest" },
  "steps":  [ Step... ],         // 每次插件调用一条，按序
  "patches": [ PatchOp... ],     // 本次准入实际生效的补丁（重放裁决携带历史补丁）
  "durationMs": 0
}
```

### Step（每步重放单元）
```jsonc
{ "order":1, "phase":"mutate|validate|commit", "plugin":"defaults",
  "patches":[PatchOp], "durationMs":2,
  "decision":"applied|skipped-open|denied|error",
  "reason":"Reason 或省略", "detail":"人类可读细节" }
```

### PatchOp（RFC6902 子集）
`{ "op":"add|replace|remove", "path":"/spec/replicas", "value":<json> }`
- `add` 到已存在键按覆盖处理；`replace` 目标必须存在；`remove` 缺失键为 no-op。
- `value` 与现值相等的写入不算变化（幂等支撑）。
- 路径为 JSON Pointer，`~0`/`~1` 转义受支持。

### Summary
对**规范化**后的文档（key 排序、整数不序列化为浮点）取 sha256。同一对象摘要稳定，
语义变更必然改变 digest。

## 2. 失败类别契约（`model.Reason`）

| Reason | `Category()` 大类 | `HTTPStatus()` | 产生位置 |
| --- | --- | --- | --- |
| `invalid_input` | input_error | 400 | 适配器/请求校验、插件解析坏对象 |
| `validation_denied` | compute_failure(策略类) | 422 | 校验器给出 deny 裁决 |
| `state_conflict` | state_conflict | 409 | 协调器/存储：名称冲突、目标缺失、UID 处理中 |
| `resource_exhausted` | resource_exhausted | 429 | 配额 hold 阶段或提交事务权威门 |
| `timeout` | compute_failure(超时) | 504 | 插件调用超过其独立 deadline |
| `illegal_path` | compute_failure(越权) | 500 | 声明路径守卫；fail-open 也必拒 |
| `compute_failure` | compute_failure | 500 | 插件返回错误、panic、链不收敛、其它基础设施错误 |

> 大类与 HTTP 状态是两个正交维度：题目要求可区分的四类
> （输入错误/状态冲突/资源耗尽/计算失败）由 `Category()` 给出；超时是计算失败大类下
> 独立可识别的 Reason（504）。

错误体契约（适配器）：
```json
{ "error":"人类可读", "reason":"invalid_input", "category":"input_error" }
```
**策略拒绝**（allowed/denied）始终返回 Response 结构（HTTP 状态按类别为 422/429）；
**适配层/基础设施错误**返回上述 ErrorBody。

## 3. 插件契约（`internal/plugins`）

```go
type Mutator interface {
    Name() string
    DeclaredPaths() []string                              // 唯一可写路径集合
    Mutate(ctx, Input) ([]model.PatchOp, error)           // 不直接改文档
    Timeout() time.Duration
    OnError() FailPolicy                                  // "open" | "closed"，必填
}
type Validator interface {
    Name() string
    Validate(ctx, Input) (Verdict, error)                 // deny 裁决 ≠ 调用 error
    Timeout() time.Duration
    OnError() FailPolicy
}
type Verdict struct { Allowed bool; Reason model.Reason; Message string }
```

- **deny 裁决 vs 调用错误**：`(Verdict{Allowed:false}, nil)` 表示插件成功判否
  （不受 fail policy 影响）；`(zero, err)` 表示插件无法决策，才按 fail policy 处理。
- 插件用 `plugins.Fail(reason, format, args...))` 给错误标注类别；未标注的普通 error
  默认为 `compute_failure`；`context.DeadlineExceeded` 在框架边界归为 `timeout`；
  panic 被 recover 为 `compute_failure`。
- `QuotaService` 端口：`Reserve(ctx, uid, kind, amount, op)` 以 `(uid,kind)` 幂等放 hold；
  `Release(ctx, uid)` 撤销未提交 hold。真实实现 `storage.SQLQuota`，
  测试实现 `plugins.ScriptedQuota`（可脚本化 timeout/compute_error/exhausted）。
- `plugins.SignedDelta(amount, op)`：CREATE 取正数全量；UPDATE 仅正增量（≤0 为 0）；
  DELETE 为 0。

## 4. 流水线→协调器结果契约（`pipeline.Result`）

```go
type Result struct {
    Allowed bool; Reason model.Reason; Message string
    Final map[string]any; Summary *model.Summary
    Steps []model.Step; Patches []model.PatchOp
}
```
`Run` 永不把插件 panic 传出。即使是拒绝，`Steps` 也完整保留（含走到拒绝点前的全部
补丁），`Final/Summary` 在链后校验拒绝时绑定被拒对象。

## 5. 存储契约（`internal/storage`）

哨兵错误：`ErrConflict`（名称/UID 冲突）、`ErrNotFound`；配额返回被
`fmt.Errorf("...: %w", plugins.ErrQuotaExhausted)` 包裹的错误。协调器用 `errors.Is`
归类为对应 Reason。

- `InsertRequest`：`ON CONFLICT DO NOTHING`，返回 existed 供重放判断。
- `Claim/ClaimUID`：只认领 pending / failed / 租约过期的 processing；新租约不可认领。
- `CommitOutcome`：单事务原子完成 资源写 + 权威容量门 + hold 消费/释放 + 请求终态 + 审计。
  容量门返回 `ErrQuotaExhausted` 时，协调器将其落为普通 `resource_exhausted` 拒绝
  （而非 500），因为这是并发竞争下权威门的正常裁决。

## 6. 协调器错误（`internal/coordinator`）

- `*InputError` → 适配器 400/`invalid_input`。
- `*StateError{Reason, Msg}` → 用其 Reason 的 HTTP 状态（名称冲突/目标缺失=409）。
- 其它 error → 500/`compute_failure`。
- 成功返回 `AdmitResult{Response, Stored bool}`；`Stored=true` 表示这是已存终态重放
  （而非本次重新计算）。

## 7. 运行日志契约（`internal/runlog`）

`<logDir>/admission-runs.jsonl`，每行一条 `runlog.Line`：`runId`（run-UTC时间-序号，
可 grep）、`time`、`request`、`decision`、`reason`、`category`、`message`、`steps`
（含每步补丁/判定）、`finalSummary`。nil `*Logger` 为空操作。
