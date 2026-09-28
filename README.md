# local-txpool — 本地账户交易池后端

一个**纯后端、纯本地合成环境**的账户模型交易池：按发送者 nonce、费用与容量管理
待执行交易，产出确定性的候选区块，支持确认、回滚、替换、过期与容量淘汰。

- 语言/框架：Python 3.11+ · FastAPI · SQLite（标准库 `sqlite3`）
- 密码学：成熟库 [`eth-keys`](https://github.com/ethereum/eth-keys)（secp256k1
  签名/公钥恢复）+ `eth-hash`/`pycryptodome`（keccak-256）
- 交易形态：EIP-155 遗留交易，**RLP 编解码在本仓库手写实现**（严格、防多编码歧义）
- 无任何生产账号、无真实业务数据、无网络依赖；账户用确定性合成密钥构造

---

## 1. 它实现了什么（核心规则）

### 1.1 可执行连续前缀 vs 有缺口等待队列

对每个发送者，从其**链上 nonce** 起按 nonce 递增扫描。费用再高，也不能跳过自身
nonce 缺口：

| 情况 | 归类 | 理由码 `reason` |
|---|---|---|
| nonce 连续、且沿前缀逐笔累计都负担得起 | `pending` | `EXECUTABLE` |
| 该 nonce 之前缺交易 | `queued` | `NONCE_GAP` |
| 前缀在某笔处因累计余额不足断裂，该笔本身 | `queued` | `INSUFFICIENT_FUNDS` |
| 断裂点之后的交易（nonce 连续也得等） | `queued` | `GAP_AFFORDABILITY` |

可负担性按**前缀投影**判断：从链上余额出发，每接受一笔 pending 就扣减其
`value + gas_limit*gas_price`。因此余额只够前两笔时，第三笔即使自身金额为 0
也不会进 pending（有专门测试 `test_prefix_projects_costs_across_nonces`）。

### 1.2 同 nonce 替换（涨价条件）

- 同 `(sender, nonce)` 在 `pending/queued/included` 中**至多一条有效交易**，
  由 SQLite 部分唯一索引 `ux_active_sender_nonce` 强制。
- 新交易必须满足 `new_gas_price >= ceil(old_gas_price * (100+bump)/100)`
  （默认 bump=10%），否则返回 `REPLACEMENT_UNDERPRICED`（HTTP 409），details
  里带 `required_gas_price`。
- 替换成功：旧交易归档 `replaced`（理由 `REPLACED_PRICE_BUMP`，并记录
  `replaced_by`），槽位释放给新交易。同 nonce 的两笔不同交易不可能并存有效。
- 完全相同的原始交易（同哈希）重放 -> `ALREADY_KNOWN`。

### 1.3 候选区块顺序

`/pool/candidate` 与 `propose` 用同一选择器：每一步在**各发送者当前队首
pending 交易**中选 `gas_price` 最高者；同价按发送者地址、再按 nonce 升序
（完全确定）。某笔因区块 gas 剩余不足放不下时，跳过该发送者（其后续 nonce
不得越过它）。带缺口/余额断裂的交易根本不在 pending 里，因此**无法**靠高价
插队跳过缺口。

### 1.4 区块确认

`propose`（pending→included，余额不动）→ `confirm`（唯一的未决提议区块原子上链）：

- 确认前在单事务内**整批预演**：逐笔校验同发送者 nonce 连续、余额充足，
  任一不满足则整体拒绝（不会半上链）；
- 对受影响账户（发送者、接收者、coinbase）先拍快照，再扣款/收款/付手续费/
  递增 nonce，交易 included→`mined`；
- 确认后对受影响发送者重新分类。

### 1.5 回滚重新分类

`rollback n` 逆转最近 n 个**已确认**区块（有未决 propose 时拒绝，
`BLOCK_CONFLICT`）：逐块恢复账户快照、删除区块、`mined` 交易重回池中重新
分类（`ROLLBACK_REEXEC`）；已过 TTL 的回滚交易标 `expired` 而非复活。
回滚后重新提议，候选顺序与原来逐笔一致（确定性，有测试断言）。

### 1.6 过期与淘汰不破坏索引

- TTL：`received_at + ttl_seconds <= now` 即过期（`EXPIRED_TTL`）。过期只改
  状态与理由并释放活跃唯一槽位，`replaced_by` 等指针保持有效。
- 容量淘汰**只针对 `queued`**（pending/included 永不被挤掉）：超出全局/账户
  queued 配额时，淘汰**比新到交易更便宜**的 queued（`EVICTED_QUEUE_GLOBAL` /
  `EVICTED_QUEUE_ACCOUNT`）；新交易不够贵挤不掉任何人时返回 `POOL_FULL` /
  `ACCOUNT_QUEUE_FULL`。

### 1.7 稳定失败类别（机器可读 code）

`BAD_REQUEST, INVALID_SIGNATURE, WRONG_CHAIN_ID, INTRINSIC_GAS, UNDERPRICED,
INSUFFICIENT_FUNDS, NONCE_TOO_LOW, NONCE_TOO_FAR, REPLACEMENT_UNDERPRICED,
ALREADY_KNOWN, ACCOUNT_QUEUE_FULL, POOL_FULL, EXPIRED, NOT_FOUND, BLOCK_CONFLICT,
EMPTY_BLOCK, BLOCK_NOT_PROPOSED, ROLLBACK_TOO_DEEP, INVALID_STATE`。
测试与黄金夹具对这些 **code 逐一断言**，而不是只断言“接口报错”。

---

## 2. 工程结构（多模块，核心无硬编码演示）

```
src/localtxpool/
├── encoding.py          # 编码与验签：手写严格 RLP + EIP-155 签名/恢复 + 固有 gas
├── core/
│   ├── mempool.py       # 链状态内核：前缀分类/替换/过期/容量淘汰/候选选择
│   └── chain.py         # 提议/确认(整批预演+快照)/丢弃/回滚
├── storage/
│   ├── schema.sql       # 表结构 + 部分唯一索引（同 nonce 不并存有效）
│   └── repository.py    # SQLite 索引存储（事务、journal、快照）
├── replay/
│   ├── runner.py        # 离线回放：事件 JSONL + 确定性虚拟时钟
│   └── golden.py        # 与人工“黄金期望”逐事件核对（差异全列出）
├── api/                 # FastAPI：app 工厂、路由、Pydantic 模型、统一错误信封
├── clock.py config.py service.py cli.py logging_setup.py asgi.py
tests/                   # 独立 pytest 套件（94 个测试）
fixtures/
├── scenarios/*.jsonl    # 人工编写的事件夹具
└── golden/*.golden.json # 人工编写的期望（不由被测核心生成）
configs/default.toml     # 配置
scripts/example.sh       # curl 端到端示例
```

### “参考答案不是被测核心自己生成的”

1. **RLP 独立预言机**：运行时手写 RLP，测试引入**第三方 `rlp` 包**互编互解
   交叉核验（`tests/test_rlp_codec.py`）；落库的 raw 字节也用第三方库重新解码
   比对（`test_stored_raw_decodable_by_independent_rlp`）。运行时代码不 import 它。
2. **黄金期望人工推导**：`fixtures/golden/*.json` 里的候选顺序、余额、nonce、
   失败类别均为手工计算写入；`golden.py` 只做子集比对，并含一个“篡改黄金必被
   发现”的测试，证明核对不是恒真。

### 可解释性

- 每个请求可带 `X-Request-ID`（不带则自动生成），响应头与响应体都回带。
- 所有状态迁移写 `journals`：`action / from_status / to_status / reason /
  detail / tx_hash / block_number / request_id`，经
  `GET /explain/journals` 按请求、交易、区块、发送者查询。
- 日志为 JSON 行，含 `request_id / action / code / component`；**失败原因**
  （`error` code + message + details）与正常结果分开承载；不确定/无法解析的
  journal detail 会打 `"uncertain": true`。

---

## 3. 安装

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt          # 运行
pip install -r requirements-dev.txt      # 含测试与第三方 rlp 预言机
```

依赖版本已锁定（见 requirements*.txt，Python 3.12 / Linux 验证；要求 >=3.11，
仅用标准库 `tomllib`）。

---

## 4. 使用

### 4.1 启动 HTTP 服务

```bash
# 方式一：CLI
local-txpool serve --config configs/default.toml
# 方式二：uvicorn 工厂
LOCALTXPOOL_CONFIG=configs/default.toml \
  uvicorn --factory localtxpool.asgi:app_factory --host 127.0.0.1 --port 8000
```

交互式文档：<http://127.0.0.1:8000/docs>。

### 4.2 离线回放与黄金核对（无需起服务）

```bash
# 打印全部 trace + 最终快照
python -m localtxpool.cli replay fixtures/scenarios/01-fee-competition.jsonl
# 回放并与人工黄金期望核对（退出码 0=通过，2=有差异并列出全部 mismatch）
python -m localtxpool.cli verify \
  fixtures/scenarios/01-fee-competition.jsonl \
  fixtures/golden/01-fee-competition.golden.json
```

事件类型见 `replay/runner.py` 顶部文档：`clock / fund / tx / raw / propose /
confirm / discard / rollback / reap / snapshot`，`at` 为虚拟时钟秒（只增不减）。

### 4.3 curl 端到端示例

```bash
./scripts/example.sh http://127.0.0.1:8000
```

### 4.4 主要 HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 版本、chain_id、当前时间 |
| `POST /admin/fund` | 合成注资/设置 nonce，并触发该账户重分类 |
| `GET /accounts`, `/accounts/{addr}` | 账户余额/nonce |
| `POST /transactions` | 提交 `0x` RLP 已签名交易（202，返回 pending/queued+reason） |
| `GET /transactions/{hash}`, `GET /transactions?status=...` | 查询 |
| `GET /pool/status` | 各状态计数、链尖、未决区块 |
| `GET /pool/candidate?gas_limit=` | **只读**候选顺序与逐笔理由 |
| `POST /pool/reap-expired` | 主动收割过期交易 |
| `POST /blocks/propose` | 生成候选区块（201） |
| `POST /blocks/confirm` | 确认唯一未决区块 |
| `POST /blocks/discard` | 放弃未决区块，交易回池重分类 |
| `POST /blocks/rollback` | body `{"n":1}`，回滚已确认区块 |
| `GET /blocks/head` | 链尖 |
| `GET /explain/journals` | 按 `request_id/tx_hash/block_number/sender` 查流水 |

统一响应信封：`{"ok": bool, "data": ..., "request_id": ..., "warnings": [...]}`；
失败时：`{"ok": false, "error": "<CODE>", "message": ..., "details": {...},
"request_id": ...}`。金额一律十进制**字符串** wei。

---

## 5. 真实执行验证（已运行）

- `pytest`：**94 passed**。覆盖：手写 RLP × 第三方 rlp 互编互解与严格畸形拒绝、
  EIP-155 链 ID/高 s/r-s 越界/尾部字节/字段数、前缀与累计余额断裂、替换涨价
  边界（ceil 取整）、容量淘汰与“便宜新交易挤不掉”、TTL 释放槽位、候选顺序、
  确认落账、丢弃、回滚状态恢复与确定性重放、唯一索引与事务回滚、HTTP 错误码
  信封与 request_id 关联。
- 三个黄金场景 `verify` 全部通过：
  - `01-fee-competition`：多账户费用竞争 + 余额/手续费落账（断言 a2 价高也
    必须排在 a1 之后）；
  - `02-gaps-replace-expiry`：nonce 缺口、余额断裂、替换涨价/同价拒绝/重放、
    included 过期、丢弃回池；
  - `03-rollback`：两区块确认、`ROLLBACK_TOO_DEEP/EMPTY_BLOCK/BLOCK_CONFLICT`
    失败类别、回滚恢复与重新提议顺序一致。
- 真实 `uvicorn` + `curl` 端到端跑通：注资 → 两账户竞争（候选 gp99 在 gp10
  前）→ propose/confirm（miner 手续费 = 21000×109 = 2289000，余额/nonce
  正确）→ 按 request_id 查 journal → rollback 后余额/nonce 全部归零恢复、
  交易重回 pending；错误路径返回 `WRONG_CHAIN_ID`、`REPLACEMENT_UNDERPRICED`。

复现：

```bash
pip install -r requirements-dev.txt
pytest -q
for n in 01-fee-competition 02-gaps-replace-expiry 03-rollback; do
  python -m localtxpool.cli verify fixtures/scenarios/$n.jsonl fixtures/golden/$n.golden.json
done
```

---

## 6. 配置摘要（`configs/default.toml`）

`chain_id=31337`、`block_gas_limit=10_000_000`、`price_bump_pct=10`、
`ttl_seconds=3600`、`max_future_nonce=64`、每账户 pending/queued 与全局配额、
SQLite 路径、JSON 日志开关。

---

## 7. 剩余限制（明确列出，不含糊）

1. **单进程/单写者**：SQLite 打开为单连接（`check_same_thread=False`），内核
   调用方需在同一事件循环/进程内使用；没有多副本并发或分布式锁。高并发写入
   依赖 `BEGIN IMMEDIATE` 串行化，但未做多进程压测。
2. **遗留交易 only**：仅 EIP-155 legacy 交易字段，未实现 EIP-2930/1559/4844
   （无 access list、无 base fee / priority fee、无 blob）。`gas_price` 是
   单一价格。
3. **gas 模型简化**：按 EIP-2028 数据定价算固有 gas（零字节 4 / 非零 16 +
   21000），**没有 EVM 执行**，不退款、不计算真实 gas_used；确认时手续费按
   `gas_limit * gas_price` 全额计（未按实际消耗退还差额）。
4. **余额覆盖检查是保守投影**：分类/替换时用“前缀 pending 全部按 max_cost
   执行”投影余额；最终权威校验在 confirm 的整批预演（任何不可执行都会整体
   拒绝，不会错误上链）。未模拟交易执行后对其他账户余额的连锁影响。
5. **无 P2P / 无真实共识 / 无真实链数据同步**：区块哈希是本地合成的
   `keccak(parent || txs || number)`；coinbase、账户余额均为本地夹具。
6. **费用维度单一**：按 `gas_price` 排序，未考虑 nonce-gap 之外的账户级
   “槽位定价”、驱逐押金或垃圾负载维度；驱逐仅针对 queued。
7. **时间为整数秒**，过期边界取 `expires_at <= now`；默认关闭后台扫描线程
   （`sweep_interval_seconds=0`），过期在提交/提议/reap 时惰性处理。
8. 回滚要求先解决未决提议区块；不支持跨“未确认提议”混合回滚，也不保留
   分叉（无 uncle/重组树），只做线性逆序恢复。
