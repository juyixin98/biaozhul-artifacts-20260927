# local-txpool：本地合成账户交易池后端

按**发送者 nonce、费用、容量**管理待执行交易的纯后端服务。所有数据与参与者
都是本地合成夹具（确定性 secp256k1 密钥、本地 chain_id `31337`、SQLite），
不连接、也不需要任何生产链或真实业务数据。

技术栈：Python 3.10+ · FastAPI · SQLite（WAL）· 成熟密码库
（`coincurve` / `eth-keys` / `eth-utils` / `rlp` / `pycryptodome`）。

---

## 1. 它精确实现了什么

| 需求 | 实现位置 | 行为 |
| --- | --- | --- |
| 可执行连续前缀 vs 有缺口等待队列 | `core/kernel.py::_reclassify_sender`、`core/ordering.py` | 每个发送者只有从账户 nonce 起、nonce 连续且累计余额可承担的最长前缀是 `pending`；其余 `queued`。 |
| **费用高不能跳过自身 nonce 缺口** | `core/ordering.py` | 候选排序只从每个发送者的 nonce"队头"里全局取最高价；缺口后的交易即使全池最高价也不可执行。 |
| 替换需明确涨价，同 nonce 不并存有效 | `core/kernel.py::_apply_replacement` | 新价必须 ≥ 旧价 ×(100+`replacement_price_bump_pct`)/100（向上取整，默认 10%）；`(sender,nonce)` 上有部分唯一索引，旧交易置 `dropped(replaced)` 后新交易才有效。已在未确认区块中的交易必须先回滚（返回 `conflict`）。 |
| 区块确认 | `core/kernel.py::_auto_confirm` | 链头下方 `confirmation_depth`（默认 3）个高度的区块最终化，交易转 `confirmed`。 |
| 回滚重新分类 | `core/kernel.py::rollback_to` | 仅未最终化区块可回滚；逆序退款、nonce 回退、交易重入池并重新应用连续前缀规则。已最终化返回 `block_rollback_finalized`。 |
| 过期与淘汰不破坏索引 | `expire_pending`、`_evict_one` + 存储事务/部分唯一索引/`assert_integrity` | 过期按"连续停留 pending 时长"（`pending_since_ms`）判定；容量淘汰先驱逐低价 `queued`，必要时才驱逐 `pending`；每步后统一重算承诺额与重分类，并在单一事务内提交。 |

### 状态机

```
                提交(重分类)               缺口闭合/回滚重入
   (入口) ──► queued ◄──────────────► pending
                │  余额截止/nonce缺口      │
                │                         │ propose_block（执行扣费、nonce+1）
                │                         ▼
                │                     proposed ──达到确认深度──► confirmed(终态)
                │                         │
                │                         │ rollback（未最终化）
                │                         ▼
                └──────────────────► rolled_back ──重分类──► pending/queued
        替换/过期/淘汰 ──► dropped(终态，行保留供审计)
```

---

## 2. 工程结构（多模块，各负其责）

```
local_txpool/
├── core/
│   ├── crypto.py      编码与验签：RLP 类遗产交易、EIP-155 链域、secp256k1 恢复、
│   │                  EIP-2 低 s、内在 gas。只做密码学，不碰池状态。
│   ├── kernel.py      链状态内核：准入、重分类、RBF、提议/确认/回滚、过期/淘汰。
│   ├── ordering.py    候选区块排序（纯函数：每发送者 nonce 队头 + 全局最高价）。
│   ├── models.py      领域模型、TxStatus、稳定 ErrorCode、审计事件。
│   ├── config.py      配置（YAML + LTXP_ 环境变量 + 默认值）。
│   └── clock.py       SystemClock / FakeClock（TTL 可确定回放）。
├── storage/
│   └── repository.py  SQLite 索引存储：schema、事务、部分唯一索引、不变量体检。
├── offline/
│   ├── oracle.py      ★独立参考预言机：仅用 dict/list 重写的一套预期语义，
│   │                  不 import 任何被测核心代码。
│   └── scenario.py    YAML 夹具执行器：同时驱动服务与预言机做**差分测试**。
├── api/
│   ├── app.py         FastAPI 路由、错误码↔HTTP、request_id 中间件、不确定结论。
│   ├── schemas.py     请求/响应模型（提交的是已签名 RLP 原始交易）。
│   ├── service.py     组装（config→storage→kernel→app）与 uvicorn 工厂。
│   └── logging_setup.py  带 request_id / 版本的结构化日志。
└── cli.py             serve / init-db / replay-scenario / replay-audit / gen-key

fixtures/              人工编写预期值的 YAML 场景（不是由被测核心生成答案）
tests/                 密码向量、内核行为、确认/回滚、API 端到端、夹具差分
scripts/demo_http.py   对正在运行的服务做完整调用演示
config.yaml            示例配置
```

**为什么答案不是"核心自己生成、自己断言"：** `offline/oracle.py` 是一份
独立、极简、可人工逐行复核的参考实现；`ScenarioRunner` 每执行一步都把真实
服务（SQLite + 内核）与预言机的账户、分类、候选顺序、区块执行序列逐项差分。
夹具里的 `expect_*` 是第二层**手写**具体断言。`tests/test_scenario_differential.py`
里还有一个"反向护栏"用例，证明检查点/差分在预期被篡改时确实会失败。

---

## 3. 安装与运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock        # 精确锁版本
python -m local_txpool.cli init-db       # 初始化 data/txpool.db（可选，启动会自建）
python -m local_txpool.cli serve --host 127.0.0.1 --port 8077
```

健康检查：`curl http://127.0.0.1:8077/health`
交互式 API 文档：启动后访问 `http://127.0.0.1:8077/docs`。

配置优先级：环境变量 `LTXP_<段>__<字段>` > YAML（`--config` 或
`LTXP_CONFIG_FILE`，默认 `./config.yaml`）> 内置默认。例如
`LTXP_POOL__MAX_TRANSACTIONS=4096`。

---

## 4. 示例调用

### 4.1 一键 HTTP 演示（另开终端，服务已在 8077）

```bash
PYTHONPATH=. python scripts/demo_http.py
```

演示：合成注资 → 提交"alice nonce0 低价 / nonce2 99 gwei 缺口 / bob 20 gwei"
→ 候选预览展示 **99 gwei 被 nonce 缺口挡住** → 出块（按费序）→ 按 `request_id`
拉审计轨迹 → 回滚 → 重入池。

### 4.2 手工 curl 流程

```bash
# 1) 创建合成账户（地址可用 `python -m local_txpool.cli gen-key` 生成）
curl -s -X POST localhost:8077/accounts -H 'content-type: application/json' \
  -d '{"address":"0x2c7536E3605D9C16a7a3D7b1898e529396a65c23","balance":1000000000000000000}'

# 2) 提交已签名 RLP 原始交易（0x 十六进制）
curl -s -X POST localhost:8077/transactions -H 'content-type: application/json' \
  -H 'X-Request-Id: demo-tx-1' \
  -d '{"raw_tx":"0xf86c8085...<RLP signed tx>"}'

# 3) 只读候选顺序（不改变状态）
curl -s localhost:8077/candidate

# 4) 出块 / 推进确认 / 回滚
curl -s -X POST localhost:8077/blocks/propose
curl -s -X POST localhost:8077/blocks/confirm
curl -s -X POST localhost:8077/blocks/rollback \
  -H 'content-type: application/json' -d '{"target_number":0}'

# 5) 按请求身份追溯完整处理轨迹（每次移入/移出理由、版本、位置）
curl -s localhost:8077/audit/requests/demo-tx-1
```

### 4.3 离线回放夹具（不需要起服务）

```bash
python -m local_txpool.cli replay-scenario fixtures/01_fee_competition.yaml
python -m local_txpool.cli replay-scenario fixtures/02_confirm_rollback.yaml
python -m local_txpool.cli replay-scenario fixtures/03_expiry_eviction.yaml
python -m local_txpool.cli replay-audit --limit 50      # 从线上库重放审计时间线
```

---

## 5. 夹具覆盖（可复核的具体结果）

| 夹具 | 构造的情形 | 断言的具体结果 |
| --- | --- | --- |
| `01_fee_competition.yaml` | 多账户费竞争、nonce 缺口、RBF 边界（+9% 拒绝 / +10% 接受）、低价/低内在 gas | 候选顺序精确为 `[b0(50g), a0'(12g), c0(5g)]`，30g 的缺口交易不入选；失败类别分别为 `gas_price_below_minimum`、`intrinsic_gas_too_low`、`same_nonce_lower_price`；每步与独立预言机差分一致。 |
| `02_confirm_rollback.yaml` | 确认深度=2、连续出块、回滚、最终化后拒绝深回滚、已用 nonce 重放 | 高度 4 时区块 2 最终化、3/4 仍 proposed；回滚退款金额、nonce 回退、重入后的 pending 集合逐笔断言；深回滚返回 `block_rollback_finalized`；重放已确认 nonce 返回 `nonce_too_low`。 |
| `03_expiry_eviction.yaml` | 余额截止、容量淘汰（queued 低价优先）、TTL 边界（299s 不过期 / 301s 过期）、过期后索引不空洞 | 余额只够 2 笔时第 3 笔 `queued_balance_cutoff`；超容时 1 gwei 的 queued 先被驱逐；旧 pending 过期后 queued 不越位、新提交交易不被旧时钟牵连。 |

---

## 6. 测试

```bash
python -m pytest -q
# 41 passed：密码固定向量 / 内核行为（具体错误码+不变量）/ 区块确认回滚 /
#            3 个 YAML 夹具差分 / ASGI 端到端 HTTP
```

- `tests/test_crypto_vectors.py`：标准私钥→地址向量、EIP-155 v、链 id 隔离、
  篡改后恢复出的发送者改变、`r=0` 退化签名拒绝。
- `tests/test_kernel_behaviour.py`：每个改变状态的用例末尾调用
  `kernel.assert_integrity()`（同 nonce 唯一、pending 连续、承诺额=Σpending 成本）。
- `tests/test_blocks_rollback.py`：退款、nonce 回退、最终化护栏。
- `tests/test_api.py`：真实 ASGI 传输，断言状态码、稳定错误码、
  `X-Request-Id` 关联、`uncertainties` 单列。

---

## 7. 可解释性约定

- **请求身份**：每个请求都有 `request_id`（请求头 `X-Request-Id` 或自动生成），
  响应体与响应头都回显；`GET /audit/requests/{request_id}` 返回该请求的全部事件。
- **关键步骤/版本/位置**：审计行含 `event_type`、`reason`、`tx_hash`、
  `block_hash`、`module`、`service_version`、`occurred_at_ms`、结构化 `detail`。
- **失败原因单列**：错误体 `error` 是稳定错误码（见 `core/models.py::ErrorCode`），
  `message` 是人读说明，`details` 给出复核数字（要求涨价额、账户 nonce、容量等）。
- **不确定结论单列**：成功但尚未最终确定的事项放在 `uncertainties`
  （例如 `block_unconfirmed`、回滚后无法重入的交易），与硬失败分开。

### 稳定错误码（节选）

`invalid_signature` · `wrong_chain_id` · `malformed_transaction` ·
`intrinsic_gas_too_low` · `gas_limit_exceeds_block` ·
`gas_price_below_minimum` · `nonce_too_low` · `nonce_too_far_ahead` ·
`insufficient_funds` · `same_nonce_lower_price` · `same_transaction_known` ·
`sender_slot_limit` · `pool_full` · `block_full` ·
`block_rollback_finalized` · `tx_not_found` · `conflict`。

---

## 8. 剩余限制（刻意简化，均为本地合成范围）

1. **费用模型是单一 legacy `gas_price`**，未实现 EIP-1559 的
   base/priority fee、EIP-4844 blob、effective tip 竞价。
2. **执行是确定性占位**：按 `gas_limit*gas_price + value` 全额预扣，
   不做 EVM 执行、不退还剩余 gas、不处理接收方合约逻辑；没有真实的
   转账到收款方记账（只有发送方扣费/回滚退款）。
3. **无网络/P2P/交易广播**，区块由 `propose_block` 直接产生；`external_raw_txs`
   仅模拟"随区块到达的外部交易"。
4. **单进程 + 单 SQLite 连接 + 写事务串行化**：足够本地复核与中等吞吐，
   不是多副本生产架构。
5. **密钥全部本地合成**（`gen-key` 或 `keccak("local-txpool/synthetic:<name>")`
   确定性派生），绝不可在任何真实网络持有资产。
6. 回滚只支持线性高度回退（单链重组），未实现 uncle/分叉选择。
7. schema 目前只有 v1；版本不匹配会直接报错而非自动迁移。
