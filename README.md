# 本地资源请求准入流水线 (Local Admission Pipeline)

一个完全本地、可重放的资源准入（admission）参考实现：**有序变换器（默认填充 → 变异插件链到不动点）→ 链后最终校验器**，配套协调循环（有界退避重试）、配额适配器、SQLite 审计/幂等存储、标准库 HTTP 接口和按运行编号（run id）组织的可重放日志。

技术栈：**Go 1.23 + 标准库 `net/http` + SQLite**（驱动为纯 Go 的 `modernc.org/sqlite`，无需 cgo）。所有外部参与者（配额账本、慢插件、抖动插件、 rogue 插件）都是本地合成夹具，不依赖任何生产账号或真实业务数据。

---

## 1. 架构与工程边界

```
HTTP (net/http)                 internal/httpapi
   │  AdmissionReview JSON
   ▼
Service（幂等、审计绑定、配额预留）  internal/service
   │
   ├─ Pipeline                    internal/admission
   │    1. Defaults     内置默认变换器，严格执行一次
   │    2. Mutators     有序变异插件，循环到对象哈希不动点（有最大轮数，禁止无限循环）
   │    3. Validators   链后最终校验（强制至少一个校验器）
   │
   ├─ Plugins（默认器/同步器/校验器 + 故障夹具） internal/plugins
   ├─ Quota ledger（本地合成外部参与者）       internal/quota
   ├─ JSON Patch（RFC6901/6902 子集，独立测试） internal/jsonpatch
   ├─ Quantity 解析（cpu/memory，独立测试）     internal/quantity
   ▼
Reconciler（有界指数退避重试）        internal/reconcile
   │
   ▼
SQLite（幂等响应 / 审计事件 / 重试队列） internal/storage
RunLogger（run-*.jsonl 可重放日志）    internal/runlogger
```

模块间契约（均在 `internal/types`）：

- **数据契约**：`Object`（CRD 风格 metadata/spec）、`Review`、`Response`、`PatchOp`、`Summary`。
- **错误契约**：所有失败归为稳定的六类，测试按类别而不是字符串断言：
  `InvalidInput`、`IllegalMutation`、`StateConflict`、`QuotaExhausted`、`ComputeFailure`、`Timeout`。
  干净的策略拒绝（deny）**不是错误**，`failureCategory` 为空。
- **审计绑定**：审计事件把请求 UID、每一步补丁（含每步 before/after 对象指纹）与**最终对象摘要**（含整体 SHA-256 指纹）绑定。

关键保证：

1. **变换器只修改声明路径**：每个插件声明 `AllowedPrefixes()`；流水线（而非插件自己）强制校验前缀，并对 `/spec/immutable` 设硬保护。越界一律 `IllegalMutation` 且 **fail-close 不可豁免**。
2. **链后必须最终校验**：没有任何校验器的流水线在构造期直接报错。
3. **重入幂等**：插件对已达目标状态的对象必须返回空补丁；链以"整轮前后对象哈希不变"为不动点收敛。
4. **禁止无限循环**：收敛轮数受 `maxMutationPasses` 限制；振荡插件最终得到 `ComputeFailure`。
5. **失败开放/关闭显式配置**：每个插件在配置文件里必须写 `FailOpen` 或 `FailClose`；超时原因单独标记 `Timeout`（区别于一般计算失败）。
6. **禁止部分变换落库**：补丁作用于工作副本；任何一步失败，返回原对象，失败轮不提交。
7. **重复调用**：相同 UID 返回已存裁决（`replayed=true`），链不再执行，配额不重复占用。

---

## 2. 首次运行（约 1 分钟）

前置：Go 1.23+（无需 gcc/cgo）。

```bash
go build ./...
go test ./... -count=1
```

启动服务（默认监听 127.0.0.1:8080，SQLite 与日志写到本地目录）：

```bash
mkdir -p data testlogs
go run ./cmd/admissiond -config configs/admissiond.json
```

另开终端，重放全部样例请求：

```bash
./scripts/replay.sh
```

健康检查与观测端点：

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/audit/recent | head
curl -s http://127.0.0.1:8080/retry/queue
```

### 实测输出结论

按顺序重放 `testdata/requests/*.json` 得到：

| 样例 | uid | 结果 | 类别/原因 |
|---|---|---|---|
| 01-create-valid | demo-happy-0001 | allowed | 默认 replicas=3；reservedCPU=500；注解同步；最终对象 immutable |
| 02-create-needs-defaults | demo-defaults-0002 | allowed | replicas/cpu/memory 全部由默认值补齐 |
| 03-duplicate-uid-replay | demo-happy-0001 | allowed, **replayed=true** | 返回 01 的已存对象，请求体差异被忽略，配额不重复占用 |
| 04-create-policy-deny | demo-policy-0003 | allowed=false | 干净拒绝：replicas=25 超出 [1,10]，category 为空 |
| 05-create-invalid-quantity | demo-invalid-0004 | allowed=false | **InvalidInput**（不可重试、终态） |
| 06-create-quota-exhausted | demo-quota-0005 | allowed=false | **QuotaExhausted**（可重试；账本容量 2000m，请求 5000m） |
| 07-create-dry-run | demo-dryrun-0006 | allowed | dryRun：不占配额、不写 UID 裁决、不置 immutable |

每轮运行在 `testlogs/run-<runId>.jsonl` 留下逐步记录（run 编号、补丁、每步指纹、判断理由）。

另有一个故障策略对照配置：`configs/admissiond-failures.json`（端口 8081），其中慢插件 **FailOpen**（超时被容忍并继续）、配额校验器 **FailOpen**，可与默认配置对照。

---

## 3. 测试

一条命令跑全部检查（vet + 单测 + 集成 + race）：

```bash
./scripts/run-tests.sh
```

### 独立单元测试（断言具体结果与失败类别）

| 包 | 覆盖 |
|---|---|
| `internal/jsonpatch` | 指针解析/转义、前缀授权、add/replace/remove、缺失父节点 vs 缺失目标、数组下标拒绝、深拷贝不修改输入 |
| `internal/quantity` | `500m/1/0.25`、`Mi/Gi/M` 等换算与非法输入 |
| `internal/quota` | 预留幂等、Check 不占容量、cpu/memory 耗尽细节字段、Release |
| `internal/admission` | 默认补齐、**相互影响插件**、重入幂等、**非法路径**、**immutable 硬保护**、**超时可区分**、FailOpen/FailClose 对照、振荡有界终止、策略拒绝无类别、UPDATE 状态冲突 |
| `internal/plugins` | 各校验器/夹具的具体类别（InvalidInput / StateConflict / QuotaExhausted）、幂等契约、慢插件遵守 ctx |
| `internal/storage` | 响应 upsert、审计新→旧排序与步骤持久化、重试队列入队/挂起/到期/重排/删除 |
| `internal/service` | 成功后摘要绑定与配额预留、**重复调用重放**、配额耗尽类别、dry-run 无副作用、非法输入终态落库 |
| `internal/reconcile` | 退避时刻表 50/100/200ms、先失败后成功（3 次尝试、审计全保留）、不可重试立即丢弃、尝试上限=4 |
| `internal/config` | 缺 failurePolicy / 零校验器 / 重名 / 未知类型被拒 |
| `internal/runlogger` | JSONL 字段、按 run 过滤、runId 列表 |

### 集成测试（真实 HTTP + 文件 SQLite + 后台协调循环 + 配置装配）

`go test ./test/integration/ -v`，共 8 个端到端用例：

1. `TestE2EHappyPathDefaultsInteractionFinal` — 默认值、相互影响、最终对象与**每一步补丁的字面断言**（含收敛轮不再变异）。
2. `TestE2EDuplicateCallReplays` — 同 UID 重放，不同请求体被忽略。
3. `TestE2EIllegalMutation` — 越界补丁返回 `IllegalMutation`，无部分提交，队列不重试。
4. `TestE2EImmutableGuard` — 直接改写 `/spec/immutable` 被硬保护拒绝。
5. `TestE2ETimeoutRetriedToCap` — 类别恒为 `Timeout`；审计恰有 4 次尝试（1+3）；4 个 run 日志文件都带 Timeout 类别与 deadline 理由。
6. `TestE2EErrorCategoriesDistinct` — 同一服务上 `InvalidInput` 与 `QuotaExhausted` 明确可区分。
7. `TestE2EImmutableUpdate` — 不变对象 UPDATE 放行，改核心字段得 `StateConflict`。
8. `TestE2ETransportValidation` — 缺 uid / 非法 operation 在传输边界 400。

> 参考结果都以字面量写在测试里（期望值、路径、类别），不是由被测核心自己生成。

最近一次完整运行：`go test -race ./... -count=1` 全部 **PASS**（详见 §5 可复现实验）。

---

## 4. 可重放日志与失败类别

日志是一行一个 JSON 对象（JSONL），按运行编号分文件。一条超时记录示例：

```json
{"record":"run-log/v1","runId":"run-rec-…-3","level":"ERROR","phase":"mutating","pass":1,
 "plugin":"slow.e2e","category":"Timeout",
 "message":"failure aborts request (FailClose): [Timeout] plugin did not return before its configured deadline"}
```

每个变异步骤还记录 `beforeHash`/`afterHash` 与 `patch`，配合审计表即可逐步重放：对象从什么指纹出发、应用了哪些补丁、为什么允许/容忍/中止。

失败语义对照：

| 情况 | 类别 | 是否重试 | 例子 |
|---|---|---|---|
| 字段格式错误 | InvalidInput | 否 | cpu=`"nonsense"` |
| 插件改了未声明路径 / 硬保护路径 | IllegalMutation | 否 | rogue 夹具 |
| 已固化对象改核心字段 | StateConflict | 否 | UPDATE 改 cpu |
| 本地配额不足 | QuotaExhausted | 是（有上限） | 5000m > 2000m |
| 插件内部错误 / 链不收敛 | ComputeFailure | 是（有上限） | flaky / 振荡夹具 |
| 插件超过配置时限 | **Timeout** | 是（有上限） | delay 夹具，20ms 时限 vs 1s 睡眠 |
| 违反取值范围（请求本身合法） | 空类别，deny | 否 | replicas=25 |

---

## 5. 可复现实验（手动制造题目要求的五类夹具场景）

除自动化测试外，可用故障配置直接观察。启动：

```bash
go run ./cmd/admissiond -config configs/admissiond-failures.json
# 慢插件超时 FailOpen：请求仍被允许，日志里有 tolerated (FailOpen) 记录
curl -s -X POST localhost:8081/admission -H 'Content-Type: application/json' -d '{
  "apiVersion":"admission.example.com/v1","kind":"AdmissionReview",
  "request":{"uid":"manual-1","operation":"CREATE",
    "object":{"apiVersion":"v","kind":"Workload",
      "metadata":{"namespace":"ns","name":"x","labels":{"team":"t"}},"spec":{"cpu":"10m","memory":"16Mi"}}}}'
```

- **超时**：上面请求中的 `demo.slow-mutator`（30ms 时限，500ms 睡眠）被 FailOpen 容忍；改成 `FailClose` 即返回 `Timeout` 并进入重试。
- **相互影响 / 默认补齐**：见 01、02 样例。
- **非法路径**：由 `fixtures.rogue`（`attack: outside|immutable`）触发，集成测试 03/04 固化。
- **重复调用**：连续发送 01 与 03 样例（同 uid）。

清空本地状态重来：`rm -rf data testlogs && mkdir -p data testlogs`。

---

## 6. 目录

```
cmd/admissiond/            启动入口（装配 + 优雅关停）
configs/                   启动配置（默认链 / 故障策略对照）
internal/
  types/                   数据与错误契约
  jsonpatch/  quantity/     纯函数基础件（各自独立测试）
  admission/               流水线核心：顺序、超时、失败策略、收敛、硬保护
  plugins/                 内置插件 + 合成故障夹具
  quota/                   本地配额适配器
  storage/                 SQLite：responses / audit_events / retry_queue
  service/                 幂等、审计绑定、配额预留、终态判定
  reconcile/               有界退避协调循环（时钟可注入）
  runlogger/               run-*.jsonl 可重放日志
  config/                  配置加载与链工厂
  httpapi/                 net/http 适配器
testdata/requests/         7 个可重放样例
test/integration/          8 个端到端测试
scripts/                   run-tests.sh / replay.sh
```
