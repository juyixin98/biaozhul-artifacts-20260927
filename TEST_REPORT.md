# 测试报告（TEST_REPORT）

本文件汇总最近一次**实际执行**的测试结果。完整原始日志（逐用例、`-race`、覆盖率）见
`reports/latest.log`（指向带运行编号时间戳的日志文件，如
`reports/test-run-20260927T203532Z.log`）。问题可凭该日志里的**运行编号、关键中间状态、
判断理由**配合 `testdata/` 夹具重放。

## 环境

| 项 | 值 |
|---|---|
| Go | go1.23.4 linux/amd64（`GOTOOLCHAIN=local`，不自动下载工具链） |
| OS / 内核 | Linux 6.8.0-90-generic x86_64 |
| SQLite 驱动 | modernc.org/sqlite v1.34.5（纯 Go，无 cgo） |
| 预言机 | Python 3.12.3（标准库，独立实现） |
| 流夹具 | seed=20260927，300 条（flows_smoke）/ 10000 条（flows_10k） |

## 结果

`go vet ./...`：PASS
`go test ./... -race -count=1`：**全部包通过，0 失败，竞态检测器无报告**

| 包 | race 测试 | 覆盖率 |
|---|---|---|
| internal/ring（分配/环/迁移/基线） | PASS | 93.7% |
| internal/replay（版本化回放/落库） | PASS | 87.8% |
| internal/config（解析/校验） | PASS | 83.3% |
| internal/flow（五元组/规范化） | PASS | 84.6% |
| internal/hashx（固定哈希向量） | PASS | 100% |
| internal/apperr（错误分类） | PASS | 93.5% |
| internal/health（探活阈值/只 down 不 up） | PASS | 75.8% |
| internal/store（CRUD/锁→STORE_BUSY） | PASS | 77.6% |
| internal/router（CAS/失效/恢复/并发） | PASS | 74.7% |
| internal/api（HTTP 端到端/并发） | PASS | 70.0% |
| cmd/* | 仅手工/E2E 覆盖，无单元测试 | 0% |

## 关键断言（具体结果，非“接口可调用”）

- **与独立 Python 预言机逐流一致**：6 个场景 × 2 个流集合，每个版本的 vnode 整数分配、
  每条流的属主哈希与成员、迁移数/停留数、原因类别计数、全量取模基线、实际流量计数全部相等。
- **最小迁移对比全量取模基线（flows_10k）**：
  - 加成员 3→4：CH 迁移 **1934/10000 (19.3%)** vs 取模 **7426 (74.3%)**；
  - 删权重成员：CH **3395 (34.0%)** vs 取模 **6602 (66.0%)**。
- **只搬受影响桶**：加成员时每条迁移流的新属主必为新成员；删成员时凡换主流其旧属主必为被删成员。
- **全部失效再恢复**：300/10000 条流在 v0→v1 全迁走（`new_ring_empty`），v1→v2 全恢复
  （`old_ring_empty`），而 v0→v2（输入完全相同）迁移**恰为 0**，且原因类别逐条正确。
- **整数舍入**：cap=10、权重 1:1:2 得 3/3/4（base 各 1，extra 2/2/3）；
  权重 1/1/100、cap=3 得各 1（两段式保证可行，朴素 floor 会超 cap）；
  多组权重×上限断言总数恒等于 cap、正权重成员 ≥1。
- **权重为 0**：成员保留身份但环上 0 vnode、500 条流无一落到它；提升为正权重后才上环。
- **全部失效/全零权重/无成员**：分别返回 `ALL_DOWN` / `ZERO_TOTAL_WEIGHT` / `NO_MEMBERS`。
- **并发**：8 读协程持续路由 + 1 写协程反复 down/up 20 轮，`-race` 下无数据竞争、终态正确。
- **资源耗尽可区分**：第二个连接持 EXCLUSIVE 写锁时，受害者在 10ms busy_timeout 后得到
  `RESOURCE_EXHAUSTED/STORE_BUSY`，与输入错误、计算失败明确区分。
- **失败回放落库**：`to_version=99` 返回 status=FAILED 且 `STATE_CONFLICT/UNKNOWN_VERSION`，
  随后可用 `GET /v1/replay/<run_id>` 取回该失败记录。
- **HTTP 分类状态码**：400（坏 JSON/坏元组/未知字段/未知流集合）、409（陈旧版本）、
  503（全失效/零权重）、200+FAILED（可分类的回放失败）均被断言。

## 失败 / 未执行项

- **失败用例：无**（本次 `--- FAIL` 计数为 0）。
- **未执行/未自动化**：
  - 真实跨主机网络转发不在范围（本服务不做数据面），未做网卡级集成测试；
  - 主动探活用可注入的假 dialer 做确定性测试，未对真实 TCP 超时做长等待的脆弱测试；
  - `cmd/flowrouter`、`cmd/genfixtures` 通过手工 E2E 验证（见下），无自动化单测；
  - 未做压力/持久化损坏恢复测试（SQLite 文件损坏属运维范畴，文档已说明取舍）。
- 开发过程中发现并修复的一个**真实缺陷**：初版哈希仅用 FNV-1a64，vnode 键共享长前缀、
  仅短后缀变化导致雪崩不足、环上聚簇，10k 语料上权重 1 的成员实际仅得约 6.6%（桶份额 25%）。
  加入 splitmix64 终结混合后变为 23.7%/25.8%/50.5%。该现象与修复由预言机黄金值捕获。

## 手工 E2E（已实际执行）

启动后经真实 HTTP 验证：同流三次固定映射；桶份额 0.25/0.25/0.5；
`down` 立即摘除（新版本、属主中不再出现该成员）；陈旧 `expected_version` → 409 VERSION；
v1→v2 回放得 76/300 迁移（原因全为 member_removed）vs 基线 197；`up` 版本化恢复到 v3；
**杀进程重启后从 SQLite 恢复到 v3**；零权重配置路由返回 503 ZERO_TOTAL_WEIGHT；
`-check-config` 对非法配置以退出码 1 失败。

## 如何复现

```bash
make fixtures          # 固定种子流集合（已提交）
make oracle            # 独立 Python 预言机重新生成黄金值
make race              # 与本报告相同的 -race 全量测试
make cover             # 覆盖率
./scripts/example_requests.sh   # 对运行中的服务做示例请求
```

如需让 Go 测试“故意失败”以验证其确实在断言具体数值，可临时改动
`testdata/scenarios/scenarios.json` 中任一权重，`TestGoldenAgainstIndependentOracle`
会因与黄金值不符而失败（黄金值由独立 Python 程序生成，不会被 Go 代码悄悄改写）。
