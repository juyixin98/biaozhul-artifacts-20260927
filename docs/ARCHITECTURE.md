# 架构

## 总览与请求生命周期

```
                ┌───────────────────────────── stdlib net/http ─────────────────────────────┐
 HTTP client ─▶ │ adapter (JSON 编解码、状态码↔失败类别、只读查询 /requests /resources)        │
                └───────────────────────────────────┬───────────────────────────────────────┘
                                                     ▼
                            coordinator.Admit (重入幂等、in-flight 去重)
                              1. 请求级校验 (model.Request.Validate)
                              2. upsert requests(pending) → 置 processing(带租约)
                                                     ▼
            ┌──────────────────────────── pipeline.Run ────────────────────────────┐
            │ 阶段 1 变换器链（有序，迭代至不动点，受 maxPasses 限制）                  │
            │   for pass 1..N:  for m in mutators(按配置顺序):                        │
            │     ops,_ := m.Mutate(doc)          // 插件只返回补丁，不直接改 doc      │
            │     深拷贝 ← patch.Apply(ops, m.DeclaredPaths()) // 声明路径守卫         │
            │        非法 → 丢弃拷贝，illegal_path，立即终止（绝不部分提交）            │
            │   本轮无变化 => 收敛；超过 N 轮 => compute_failure                        │
            │ 阶段 2 最终校验（链收敛后，按序执行一次）                                  │
            │   schema → quota(放配额 hold)；deny/error 立即终止                        │
            └───────────────────────────────────┬───────────────────────────────────┘
                                                     ▼
              storage.CommitOutcome（单 SQLite 事务）
                allowed : upsert/delete resources → 权威容量门(对真实用量求和) → 消费 hold
                          requests=allowed/denied，写 audits
                denied  : 撤销在途 hold，requests=denied，写 audits（含被拒对象摘要）
                                                     ▼
              runlog 追加一行（runId + 每步补丁 + 类别 + 最终摘要）

 后台：coordinator.RunReconcile 周期性 Claim pending / 租约过期的 processing 行 → 同一 process()
```

## 关键设计决策

### 1. 变换器只能“描述”修改，框架负责“应用”

插件实现 `Mutate(ctx, Input) ([]PatchOp, error)`，**拿不到可写的文档引用去越权改写**。
框架在 `patch.Apply` 中：

1. 校验每个目标路径 ∈ 插件 `DeclaredPaths()`（支持精确路径与 `.../*` 子树）；
2. 拒绝受保护路径（`/apiVersion`、`/kind`、`/metadata/name`、`/metadata/namespace`）；
3. 应用到**深拷贝**；任何一步失败就丢弃拷贝，工作文档保持不变。

这同时满足“只修改声明路径”和“禁止直接提交部分变换”。

### 2. 不动点迭代 + 硬上限

插件之间会**相互影响**：`capacity` 依赖 `defaults` 补出的 `replicas`。链按序多轮执行，
直到一整轮无人产出补丁（收敛）。为防止非幂等插件让链永不停止，迭代受 `maxPasses` 限制，
超限返回明确的 compute 失败。`TestNonConvergingMutatorBounded` 用一个每轮翻转取值的
振荡插件，断言它**恰好**运行 `maxPasses` 次后被终止。

### 3. 幂等的两个层面

- **补丁层幂等**：值相同的写入和“删除不存在的键”都不产生变化；内置变换器观察到目标态
  即返回空补丁。因此对“已完成对象”再跑一遍链，补丁为零、digest 不变。
- **请求层幂等**：同一 UID 第二次提交直接重放已存终态裁决（不新增审计 run、不重复占配额）。
  配额 hold 以 `(uid, kind)` 为主键幂等插入。

### 4. 失败策略显式、失败类别正交

- 每个插件在启动配置里必须显式写 `failPolicy: open|closed`（无默认值，配置加载即校验）。
  - 插件**调用失败**（超时/panic/返回 error）：open = 记录 `skipped-open` 并继续，
    closed = 拒绝。
  - 插件给出**拒绝裁决**（deny verdict）与调用失败是两回事：策略拒绝不受 fail policy 影响。
  - **越权路径恒为致命**：fail-open 永远不能放行一次越界写入。
- 失败类别用一个枚举 `model.Reason` 表达，并映射到类别大类与 HTTP 状态码，
  输入错误 / 状态冲突 / 资源耗尽 / 计算失败 / 超时 彼此可区分。

### 5. 超时是独立类别

每个插件调用在独立 context 超时下运行（goroutine + select），panic 被 recover 成
`compute_failure`，只有插件自己的 deadline 到期才是 `timeout`(504)；父 context 取消仍是
`canceled`，二者不混淆。

### 6. 配额：早期 hold + 提交时权威门（两段式）

- 校验阶段 `quota` 校验器放一个**在途 hold**，提前给出友好拒绝，并让并发请求互相看到余量；
- 权威容量检查在 `CommitOutcome` 的**同一事务**、在 resources 写入之后进行：
  `SUM(committed resources.replicas) + SUM(其他请求在途 hold.delta)`，
  扣除本请求自身的 hold（它此刻已由资源行代表）。
- hold 只代表在途占用，提交即删除，账本不累积历史。这保证：
  committed 用量恒等于真实资源副本数 —— UPDATE 不会重复计入旧 CREATE，
  DELETE 立即释放，删除后可按满额重建。SQLite 单写连接串行化，两个超配请求不可能同时提交。

### 7. 请求与审计绑定最终对象摘要

最终对象经规范化（map key 排序、整数不归一化成 float）后取 sha256，得到 `Summary`。
该摘要同时写入：响应 `finalSummary`、`requests.final_digest`、`audits.summary_digest` 与
审计 JSON、运行日志。被拒绝时也对“链后最终对象”算摘要，使审计能说明“拒绝的到底是什么”。

## 存储表（SQLite）

- `requests(uid PK, status, payload, reason, message, final_digest, attempts, lease_until, …)`
  状态机：pending → processing（带租约）→ allowed/denied；failed 可被协调器重试。
- `resources(kind, namespace, name PK, uid, doc, replicas)`：已提交对象（权威用量来源）。
- `audits(id, uid, run_id, status, reason, summary_digest, record JSON)`：每次准入完整重放。
- `quota_ledger((uid,kind) PK, delta, committed)`：仅在途 hold，提交后删除。

## 并发模型

- SQLite `SetMaxOpenConns(1)`：天然串行化所有写事务，简化配额/状态竞争推理。
- HTTP 处理可并发；协调器用进程内 `inflight` 集合防止同一 UID 在本进程被同步路径与
  协调循环双重处理；跨进程则靠租约 + 单写者。
