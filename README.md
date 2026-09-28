# 链重组派生索引（reorgindex）

对一个**本地合成区块链**实现的、可审查的**链重组（reorg）感知派生索引**：

- 区块按**父哈希**连接；父区块未知时**先挂起**，父到达后按序排空；
- 固定的**分叉权重规则**决定最佳链：分叉点之后的累计权重严格更大才切换，
  等权保留当前链；
- 切换严格**先撤旧、后加新**，全程单事务，任何中途失败整体回滚，
  查询永远只读到某一条**完整链版本**；
- 同一笔交易（`tx_id`）无论在一条链重复出现还是同时出现在两条分叉上，
  **最多产生一个有效贡献**；
- **最终性边界以内的重组按模型明确拒绝**（`finality_reorg`），状态不变。

技术栈：Python 3.10+ / FastAPI / SQLite / `cryptography`（Ed25519、双 SHA256）。
全部数据与参与者均为本地合成夹具，无任何生产账号或外部服务。

---

## 1. 目录结构（模块有真实职责，非单文件脚本）

```
src/reorgindex/
  crypto.py       编码/规范化哈希/默克尔根/Ed25519 签名验签（纯函数，无状态）
  models.py       pydantic 数据模型（API/夹具边界校验）
  errors.py       稳定错误类别：decode/verification/consensus_rule/
                  unknown_parent/duplicate_block/finality_reorg
  config.py       配置（最终性深度 D、等权策略、脱敏长度）
  diagnostics.py  结构化 JSON 诊断 + 敏感字段脱敏
  consensus.py    固定规则：累计权重比较、最终性、回滚边界（纯函数）
  storage.py      SQLite：区块/交易/悬挂池/派生贡献与余额/诊断与重组审计
  kernel.py       链状态内核：验证→连接→挂起→选链→撤旧加新→查询
  reference.py    独立参考实现（不导入内核/存储/共识），用于交叉核对
  replay.py       离线 JSONL 回放 + 从存储全量重建并逐项核对
  fixturegen.py   确定性夹具与"独立期望"生成器（固定密钥种子）
  api.py          FastAPI：提交区块、链状态、确认深度、余额、诊断、重建
  cli.py          命令行：generate-fixtures / replay / rebuild-check / serve
tests/            按主题组织的独立测试（断言具体结果与失败类别）
fixtures/         最小合成夹具 *.jsonl + 独立预言机产出的 *.expected.json
config/example.env
scripts/          服务 curl 示例、离线复现脚本
test-results/     一次真实运行保留下来的可复核结果（pytest、回放输出）
```

## 2. 规则定义（评审基线，固定且可直接对照代码）

**哈希与身份**

- `tx_id = sha256d(canonical_json(交易体))`，交易体含
  `sender_pubkey, recipient, amount, nonce, memo`，不含签名。
- 地址 `address = sha256(公钥)`。
- 区块头参与哈希字段：`version, prev_hash, height, merkle_root, weight, proposer`；
  `block_hash = sha256d(带域名分隔的规范化头)`。创世块免签名，其余块需提议者
  对 `block_hash` 的 Ed25519 签名。
- 默克尔根：两两 `sha256d`，奇数节点复制末节点；空树为 32 个 0。

**连接与挂起**

- 高度必须 `parent.height + 1`，否则 `consensus_rule`。
- 父未知（非创世）→ 进入悬挂池，返回 `pending`（**不是拒绝**）；父到达后
  BFS 按挂起先后顺序排空。

**分叉权重**

- 每块有正整数 `weight`；比较两条链只统计**最近共同祖先（分叉点）之后**
  各自段的累计权重（共同部分抵消）。
- 候选段 **严格大于**才切换；相等保留当前权威链。

**最终性与重组边界**

- D = `finality_depth`（默认 6）。最终确定判定：
  `tip_height - block_height >= D`。
- 确认深度（confirmations）：链尖自身为 1。
- 设旧链尖高度 h、回滚 r 块：被撤最老块高度 h-r+1，其确认数为 r。
  当 `r >= D+1` 必然撤掉一个已最终确定块。因此 **`r <= D` 允许，
  `r > D` 拒绝为 `finality_reorg`**，且该块不入链、派生索引不变。

**派生索引（可撤回）**

- 沿当前最佳链从创世到链尖，把每笔交易金额累加给 `recipient`。
- `derived_contributions.tx_id` 有唯一约束：同一 `tx_id` 全局只计一次
  （同链重复、跨分叉重复都不会产生第二个贡献）。
- 切换时对断开块逐块 `unapply`（只撤该块真正贡献过的交易），再对连接块
  逐块 `apply`；全部在同一 `BEGIN IMMEDIATE` 事务内。

## 3. 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt        # 或 pip install -e .[test]

# 确定性夹具（密钥固定，可重复生成得到逐字节相同结果）
PYTHONPATH=src python -m reorgindex.cli generate-fixtures fixtures

# 运行全部测试
PYTHONPATH=src python -m pytest tests/ -q

# 离线回放某个夹具，并自动做"在线索引 vs 全量重建"核对
PYTHONPATH=src python -m reorgindex.cli replay data/demo.sqlite3 \
    fixtures/short_fork_wins.jsonl
```

一键离线复现（生成夹具、四个场景回放、pytest，结果落 `test-results/`）：

```bash
bash scripts/reproduce_offline.sh
```

起 HTTP 服务并跑端到端 curl 示例：

```bash
bash scripts/curl_examples.sh
```

手动起服务：

```bash
PYTHONPATH=src REORG_FINALITY_DEPTH=6 \
  python -m uvicorn reorgindex.api:create_app --factory --port 8000
```

## 4. HTTP 接口（节选）

| 方法/路径 | 说明 |
|---|---|
| `POST /blocks` | 提交区块；200 接受，202 挂起，4xx 拒绝（见 error.category） |
| `GET /chain/state` | 链尖、权威链、最终性高度、全部余额、挂起数 |
| `GET /chain/blocks/{hash}` | 区块原文 |
| `GET /chain/blocks/{hash}/confirmations` | 确认深度、是否在权威链、是否最终确定 |
| `GET /balances/{address}` / `GET /balances` | 派生余额 |
| `GET /diagnostics?limit=N` | 接受/拒绝/无法判定的结构化原因 |
| `GET /reorgs` | 重组事件与回滚区间 |
| `POST /debug/rebuild-check` | 在线索引与全量重建逐项核对 |

每个响应带 `X-Request-ID`（取自入站头或自动生成）；拒绝响应体形如：

```json
{"ok": false, "request_id": "deep-fork",
 "error": {"category": "finality_reorg",
           "message": "重组需回滚 7 个区块，超过最终性深度 6，拒绝",
           "context": {"rollback_count": 7, "finality_depth": 6, "...": "..."}}}
```

错误类别 → HTTP：`decode_error=400`、`verification_error/consensus_rule=422`、
`duplicate_block/finality_reorg=409`、未知父挂起=`202`。**测试与评审请按
`category` 判定，HTTP 码仅为辅助。**

## 5. 四个复核场景与期望

夹具与 `*.expected.json`（由**独立预言机** `reference.py` 离线产出，不是
被测内核自己给答案）：

1. **short_fork_wins**：高 2 分叉块权重 3 > 旧段 1，切换；精确回滚区间
   只含旧高 2 块（不撤分叉点/创世），撤旧加新后余额正确。
2. **deep_fork_rejected**：权威链到高度 8，从高度 1 发起的高权重分叉需回滚
   7 块 > D=6，拒绝类别 `finality_reorg`，链尖与余额不变。
3. **duplicate_tx_across_fork**：同一已签名交易同时存在于两条分叉，切换后
   收款人只增加一次（贡献数=2 而非 3）。
4. **pending_drain**：区块逆序到达（高2→高1→创世），前两块挂起，创世到达
   后一次排空成完整链。

另含**随机化交叉验证**（12 个种子，tests/test_rebuild.py）：每棵随机区块树
在每个提交序列下，内核对链、余额、贡献块、最终性拒绝次数都必须等于独立
预言机，并且等于从存储全量重建的结果。

## 6. 测试如何独立于被测实现

- 密码学测试使用 RFC 8032 Ed25519 已知向量与篡改用例，断言具体异常类别。
- 链场景的期望值（余额、回滚高度、贡献数）在测试中**手工给定**。
- 夹具的整链期望由 `reference.py` 计算——该文件刻意不 import `kernel/
  storage/consensus`，只复用哈希/验签原语，自行实现选链/最终性/去重。
- `rebuild` 用库内**原始区块**在新内存库按接收顺序重放，验证增量索引与
  从零重放一致；再由 reference 第三方交叉验证，三重独立。

## 7. 诊断与脱敏

- 每条诊断含 `event_id`、`request_id`、事件名与关键链状态
  （height、block_hash、段权重、rollback_count 等），说明为什么接受/拒绝/
  无法判定；拒绝原因持久化在 `diag_events` 表，重组在 `reorgs` 表。
- 敏感键（address/sender/recipient/pubkey/signature 等）经
  `diagnostics.scrub` 脱敏，只保留首尾少量字符；测试断言完整地址不会出现在
  日志输出中。

## 8. 持久化与并发说明

- SQLite WAL，`BEGIN IMMEDIATE` + 进程内锁串行化写；切换在单事务内
  "撤旧→加新→更新链尖→写重组审计"，中途崩溃由事务回滚保证无混合状态
  （见 `tests/test_switch_interrupt.py` 的故障注入）。
- 重新打开数据库会恢复派生表与链尖，并可立即通过 `rebuild-check` 复核。

## 9. 依赖锁定

- `requirements.txt`：直接依赖固定版本；`requirements.lock`：完整传递锁定
  （pip freeze），用于完全可复现安装。
