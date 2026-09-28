# 测试指南

测试分两层：`internal/**` 是**独立单元测试**（每个包只依赖自己的包与确定性夹具），
`tests/integration` 是**端到端集成测试**（真实配置文件 + 真实 SQLite 文件 + httptest +
重启 + 后台协调循环）。失败夹具在 `internal/plugins/scripted.go` 与 `scripted_quota.go`，
与被测核心相互独立——参考答案不允许由被测核心自己生成。

## 运行

```bash
go test ./...                  # 全部单元 + 集成
go test -race -count=1 ./...   # 竞态检测、禁用缓存（CI 推荐）
go test -v ./internal/pipeline # 查看每个用例名与耗时
go test -run TestNonConvergingMutatorBounded ./internal/pipeline -v
go test -cover ./internal/...  # 覆盖率（核心 pipeline > 94%）
```

## 真实输出结论（本机 go1.23.4 linux/amd64）

`go test -count=1 ./...`：

```
ok  	admission/internal/adapter
ok  	admission/internal/config
ok  	admission/internal/coordinator
ok  	admission/internal/model
ok  	admission/internal/patch
ok  	admission/internal/pipeline
ok  	admission/internal/runlog
ok  	admission/internal/storage
ok  	admission/tests/integration
```

48 个顶层测试全部 PASS，`-race` 下无数据竞争。核心包覆盖率：
`pipeline 94.5%`、`runlog 82.6%`、`model 73.4%`、`patch 70.8%`、`config 70.6%`、
`coordinator 67.9%`、`storage 57.9%`（存储与适配的大量路径由端到端集成测试覆盖，
集成测试在外部包不计入 `-cover` 数字）。

## 题目要求的验证过程 → 对应测试

| 要求保留的验证过程 | 测试 | 断言的具体结果 |
| --- | --- | --- |
| **用默认值补齐** | `pipeline.TestHappyPath_DefaultsThenInteraction` | 空 spec 最终 `replicas=1, schedule=always, capacity=100`，UID 注解落位；精确断言 4 条补丁路径 |
| **相互影响插件** | 同上 / `TestPluginInteractionOrdering` | capacity 读到 defaults 补出的 replicas（1×100=100；改默认为 3 则 3×10=30）；顺序颠倒时 capacity 先跑得到 compute_failure |
| **非法路径修改** | `patch.TestApply_IllegalPath*`、`pipeline.TestIllegalPathAlwaysFatal` | 写 `/metadata/name` 等返回 `illegal_path`，**fail-open 也必拒**；步骤最终判定为 error 而非 applied；原对象无半成品 |
| **超时夹具** | `TestMutatorTimeoutOpenVsClosed`、`TestValidatorTimeoutCategory` | closed 超时→denied/`timeout`(504)；open 超时→步骤 `skipped-open` 且整体放行；与 panic 的 compute_failure 区分 |
| **重复调用夹具** | `TestReentryIdempotent`、`coordinator.TestAdmit_DuplicateCallIsIdempotent`、`integration ...DuplicateReplaysSameDigest` | 二次准入补丁为 0、digest 相同；HTTP 重放不新增审计 run；真实用量不双计 |
| **核验最终对象及每步补丁** | happy path + `integration.TestHTTP_AuditEndpointAndRunLog` | 逐步校验补丁 op/path/value/顺序；审计端点与 JSONL 含 runId 与≥4 个步骤 |
| **禁止直接提交部分变换** | `patch.TestApply_RejectedSequenceLeavesCandidateUntouched` | 首补丁合法、次补丁非法时，原工作对象无任何改动 |
| **禁止无限循环** | `TestNonConvergingMutatorBounded` | 振荡插件**恰好**调用 `maxPasses` 次即终止，返回含 "converge" 的 compute_failure，2s 内完成 |
| **输入错误可区分** | `model.TestRequestValidate`、`integration.TestHTTP_FailureCategoriesAreDistinguishable` | 缺 uid → 400/input_error |
| **状态冲突可区分** | `storage.TestCommitOutcome_CreateNameConflict`、`coordinator ...NameCollision`、集成同名创建 | 不同 UID 同名 CREATE → 409/state_conflict |
| **资源耗尽可区分** | `TestQuotaExhaustionAndAdapterError`、`storage.TestSQLQuota_*`、集成 429 用例 | 超额 → denied/resource_exhausted(429)；适配器损坏 → compute_failure(500)，两者分开 |
| **计算失败可区分** | `TestPanicRecoveredAsComputeFailure`、`TestNonConverging...`、超时对拍 | panic=compute_failure；超时=timeout；链不收敛=compute_failure |
| **配额 UPDATE/DELETE 精确** | `storage.TestSQLQuota_UpdateAndDeleteKeepAccountingExact`、`coordinator.TestAdmit_UpdateDeleteQuotaAccountingExact` | 3→5 后用量=5（非 8/10）；DELETE 后=0；删除重建可按满额 5 成功；再 +1 拒绝 |
| **崩溃恢复** | `integration.TestReconcile_RecoversPendingRow`、`storage.TestClaim_ReclaimsStaleLease` | 仅落 pending 行后由协调循环认领并提交；新鲜租约不可认、过期可认 |
| **重启重放** | `integration.TestRestart_ReplaysVerdictFromSQLite` | 关闭进程、用同一 db 文件重建后，旧 UID 重放相同 digest，不重算 |
| **绑定最终摘要** | `coordinator.TestAdmit_HappyPathCommitsAndBindsSummary` | 响应、资源 doc、审计三处 digest 一致；拒绝也带被拒对象摘要 |

## 如何重放一个问题

1. 在 `data/runs/admission-runs.jsonl` 中按失败类别或 UID 过滤：
   ```bash
   grep '"category":"resource_exhausted"' data/runs/admission-runs.jsonl
   grep '"runId":"run-…"' data/runs/admission-runs.jsonl   # 定位具体运行编号
   ```
2. 该行含完整 `request` 与每一步 `steps`（补丁、decision、reason、耗时）。把 `request`
   重新 POST 到 `/v1/requests`：已终态 UID 会重放原裁决；改一个新 UID 即可用相同输入复现。
3. `GET /v1/requests/{uid}/audits` 查看持久化的同一份重放记录。
