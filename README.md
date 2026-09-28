# EIP-1559 式基础费用递推模型（合成 / 离线后端）

一套从空目录构建的多模块 Python 后端，用**真实机制**（不是硬编码演示）实现：
对**本地合成区块负载**做 EIP-1559 式基础费用递推与交易有效性检查。

- **无真实链连接、无经济预测**：下一块基础费用只是父块参数的纯函数。
- 所有账户、密钥、交易、区块均由固定种子在本地合成，无生产账户、无真实业务数据。
- 技术栈：Python 3.12 · FastAPI · SQLite · 成熟密码库（`coincurve`/libsecp256k1、`pycryptodome` Keccak-256）。

---

## 1. 架构与职责分层

```
合成负载(JSON/原始交易字节)
        │
        ▼
basefee_model/encoding   编码与验签层
        │   rlp.py        自研 RLP 编解码（严格、抗畸形输入）
        │   crypto.py     Keccak-256、secp256k1 可恢复签名、地址派生
        │   transaction.py  legacy(type 0)/EIP-1559(type 2) 签名哈希、编解码、发送方恢复
        │   merkle.py     交易 keccak 二叉默克尔根
        ▼
basefee_model/core       链状态内核（不依赖任何 I/O）
        │   fees.py       EIP-1559 基础费用递推（纯函数）
        │   validation.py intrinsic gas、费帽关系、uint256 溢出、nonce、余额
        │   state.py      账户账本、区块校验与执行、费用守恒
        ▼
basefee_model/storage    索引存储
        │   store.py      SQLite（区块/交易表、发送方与区块索引、审计聚合）
        ▼
basefee_model/replay     离线回放
        │   replay.py     从 genesis 确定性重放、严格按父块顺序、幂等+哈希校验
        │   events.py     结构化、按请求关联的日志（stderr，单行 JSON）
        ▼
basefee_model/api        FastAPI 接口
            main.py      /health /version /basefee/next
                         /transactions/validate-fee /replay /store/*
        basefee_model/cli.py   命令行离线入口
        basefee_model/fixtures/ 可复用合成夹具（确定性身份、负载画像、JSON 导出）

tests/   独立测试；tests/oracle 是不导入被测内核的独立参考实现
tests/vectors/hand_vectors.json  手算基准向量（不经过被测代码生成）
scripts/verify.py  独立端到端验证脚本（四节，退出码非零即失败）
```

核心机制全部由代码计算：递推、验签、记账、默克尔根、守恒都不是演示常量。

---

## 2. 固定参数、整数除法方向与最小增量

固定常量（`basefee_model/config.py`）：

| 参数 | 值 |
|---|---|
| `ELASTICITY_MULTIPLIER` | `2`，gas 目标 = `gasLimit // 2` |
| `BASE_FEE_CHANGE_DENOMINATOR` | `8`，单块最多 1/8 |
| `MIN_BASE_FEE` | `0`（永不低于零） |
| `INITIAL_BASE_FEE` | `1_000_000_000`（1 gwei，创世） |
| `DEFAULT_GAS_LIMIT` | `30_000_000`（合成网络，目标 15,000,000） |
| intrinsic gas | 21000 + 4/零字节 + 16/非零字节（EIP-2028） |
| 整数上界 | `uint256`，费帽/预留成本超出即判无效 |

递推（逐字对齐 go-ethereum **v1.10.26** `consensus/misc/eip1559.go::CalcBaseFee`，
已核对上游源码）：

```
target = gasLimit // 2

used == target  -> next = parentBaseFee
used >  target  -> delta = (used-target)*parentBaseFee // target // 8
                   next  = parentBaseFee + max(delta, 1)     # 上调最小增量 = 1 wei
used <  target  -> delta = (target-used)*parentBaseFee // target // 8
                   next  = max(parentBaseFee - delta, 0)      # 下调夹到 0
```

**整数除法方向**：所有操作数非负，`//` 为向下取整（floor）；本场景下与“向零截断”结果相同。
两个易被浮点/连续近似掩盖的整数边界被显式测试：

- **最小上调增量 1 wei**：`parentBaseFee=1, used=full` 时整除 delta=0，强制 +1 → `2`；
  `parentBaseFee=0, used=full` → `1`。
- **下调截断不越负**：`parentBaseFee=3, used=target-1` 时 delta 整除为 0，next 仍为 `3`；
  `parentBaseFee=1, used=0` → 夹在 `1`。

**区块 gas 使用不能超过上限**：`used > gasLimit` 直接判 `block_gas_over_limit`；
单交易执行 gas 必须满足 `intrinsic ≤ gas_used ≤ tx.gas_limit`。

### 费帽如何共同决定实际扣费

- type-2：`effective = min(max_fee, base_fee + priority)`；
  `tip = effective − base_fee`，`burn_per_gas = base_fee`。
- legacy：`effective = gas_price`；`tip = max(0, gas_price − base_fee)`。
- 校验：`max_fee ≥ priority`、`max_fee ≥ base_fee`（否则交易无法支付）、
  `gas_price ≥ base_fee`（legacy），以及 `fee_cap*gas_limit + value` 不溢出 uint256。

### 下一块费用只依赖父块

`next_base_fee(parent_base_fee, parent_gas_used, parent_gas_limit)` 是无状态纯函数，
不读取更早历史、不读取交易内容、不读取任何外部输入（测试 `test_only_parent_determines_next_base_fee` 固化此性质）。

---

## 3. 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 全部精确锁定版本

# 独立验证脚本（手算 × 独立预言机 × 被测系统）
python scripts/verify.py                 # 退出码 0 即全部通过

# pytest（59 个用例，断言具体数值与失败类别）
python -m pytest -q

# 命令行离线回放合成区块到 SQLite
python -m basefee_model.cli replay data/canonical_fixture.json --db run.db --request-id run-001
python -m basefee_model.cli timeline --db run.db
python -m basefee_model.cli totals   --db run.db
python -m basefee_model.cli next --parent-base-fee 1000000000 \
    --parent-gas-used 0 --parent-gas-limit 30000000

# HTTP 服务
uvicorn basefee_model.api.main:app --host 127.0.0.1 --port 8000
```

环境变量：`BASEFEE_DB_PATH`（持久化 SQLite 路径）、`BASEFEE_CHAIN_ID`、
`BASEFEE_GENESIS_BASE_FEE`。

---

## 4. 接口可解释性（请求身份 / 关键步骤 / 版本 / 处理位置）

每个响应都带：

- `request_id`：由请求头 `X-Request-Id` 提供，否则自动生成；响应头原样回带，
  且该请求的**每条结构化日志**都带同一 id，可端到端关联。
- `version`：模型版本（响应头 `X-Model-Version` 同步）。
- `steps[]`：关键步骤，含处理位置（如 `basefee_model.core.fees.next_base_fee`）、
  说明与中间量（target/direction/delta）。
- `failures[]` 与 `uncertainties[]`：**严格分列**。失败是分类明确、阻断执行的错误；
  不确定是不阻断的语义提示（例如 legacy 交易把单一 `gas_price` 同时当费帽与优先费），
  单独成列、绝不混进失败。

错误分类（`errors.FailureCode`，测试逐一断言具体类别，而非“接口能调通”）：
`malformed_rlp / unsupported_tx_type / bad_signature / signer_mismatch /
chain_id_mismatch / invalid_fields /
gas_limit_exceeds_intrinsic / fee_cap_less_than_priority /
fee_cap_below_base_fee / fee_cap_overflows_u256 / priority_cap_overflows_u256 /
nonce_too_low / nonce_too_high / insufficient_funds /
block_gas_over_limit / block_gas_negative / bad_base_fee /
bad_block_number / bad_parent / duplicate_block / empty_chain`。

签名恢复出的发送方必须匹配，且交易签名域中的 `chain_id` 必须等于本链
（EIP-155/EIP-1559 防重放），不匹配判 `chain_id_mismatch`。

日志写到 stderr，单行 JSON：`ts/level/event/component/version/request_id/...字段`；
错误为 `level=error`，不确定为 `level=warning` 并带 `uncertainty` 字段。

---

## 5. 验证设计：参考答案不是被测核心自己生成的

三重独立来源必须一致（`tests/test_fees.py`、`scripts/verify.py` 第 1 节）：

1. **手算基准** `tests/vectors/hand_vectors.json`：期望值由逐式整数手算写入，
   文件 `provenance` 记录方法与对齐的 geth 版本；损坏 JSON 也无法静默通过——
   关键锚点在测试里还以字面量再次断言。
2. **独立参考预言机** `tests/oracle/reference_oracle.py`：按 EIP 叙述**另写一遍**
   费用递推与有效价格，**刻意不 import `basefee_model.core`**，与实现无共享代码。
3. **被测系统** `basefee_model.core`。

覆盖案例（对应需求第三阶段）：

- 目标负载、空块、满块、3/4 与 1/4 填充；
- **极低基础费**手算向量（base=0/1/3/8/16，含最小增量与夹零/截断）；
- **多区块递推**（target→full→full→empty→empty→target 等 7 块，逐块 base fee 与头部值）；
- **费用守恒**：`sender_debit == burned + tips + transferred(value)`，单交易与整链两层断言；
- **溢出**：费帽超 uint256、`cap*gas+value` 超 uint256；
- **无效费帽**：`cap<priority`、`cap<base_fee`、legacy `gas_price<base_fee`，均断言确切 `code`。

`data/canonical_fixture.json` 是可复用的合成链（确定性签名交易 + 每块执行 gas），
CLI、API、pytest、verify.py 复用同一份负载。

---

## 6. 边界语义（明确“模型不是什么”）

为避免过度承诺，以下边界是有意为之，而非缺陷：

1. **不连任何真实链**。无 RPC/P2P/节点客户端；区块来自本地夹具。
2. **不做经济预测 / 不做 gas 竞价预测**。模型只执行确定性的 EIP-1559 公式，
   不预测未来 base fee、需求或价格。
3. **不执行 EVM 字节码**。每笔交易的“已执行 gas”是夹具给出的合成执行结果，
   但仍强制 `intrinsic ≤ used ≤ gas_limit` 且区块总和 `≤ gasLimit`；状态转移只做
   余额/nonce 与费用（burn/tip/value）记账。
4. **默克尔根**是交易哈希上的 keccak 二叉默克尔树（奇数节点复制上提），
   不是以太坊完整的 hex-prefix 状态/收据 trie；足以保证交易增删/改序被检测，
   但不声称与主网 receipt-root 字节级一致。
5. **区块头哈希**绑定 number/parent/base_fee/gasLimit/gasUsed/txRoot 六个字段，
   是用于父链与防篡改的简化头，不是主网完整区块头（无时间戳、难度、ommers 等）。
6. **合约创建交易**（`to` 为空）在本模型中无 EVM，其 `value` 计入 burned（不可交付），
   守恒等式仍严格成立；普通转账的 `value` 正常交付给收款方。
7. **nonce 严格连续**（不做 mempool 乱序/缺口容忍）；**强制 EIP-155 chain_id**
   （防跨链重放，默认 chain_id=1559）；签名接受 secp256k1 全范围 s
   （不强制 low-S/EIP-2 规则），但 r/s 越界或为零会被明确拒绝。
8. **矿工 tip 只记账不派发**：模型没有 coinbase 账户，tip 计入聚合统计
   （`tips`），不新增余额；burned 被锁定记录、不流通。总供应视角因此：
   `Σ sender_debit = Σ burned + Σ tips + Σ delivered_value`。
9. **Wei 金额按 uint256 处理**：SQLite 因 64 位 INTEGER 容不下 uint256，
   金额列以十六进制 TEXT 存储、读取转回 Python 大整数，聚合在 Python 中完成。

---

## 7. 未能执行 / 不适用的检查（单列，不计为通过）

本次在本地 Linux + Python 3.12 环境中实际执行并通过的项目见 `scripts/verify.py`
与 pytest 输出。以下检查**没有执行**，明确列出，**不声称已通过**：

- **跨平台/其他 Python 版本**验证（仅在 Linux x86_64 / Python 3.12.3 实测；
  Windows、macOS、3.10/3.11 未运行）。
- **libsecp256k1 的形式化/FIPS 合规审计**：信任 `coincurve` 上游随包的原生库，
  本仓库未对其做密码学审计或从源码重编译校验。
- **与真实主网区块的逐字节对账**：按设计无真实链连接，未也不应在此模型内做；
  仅以 geth v1.10.26 的 `CalcBaseFee` 源码语义做人工对齐。
- **并发写/多进程写入同一个 SQLite 文件**的压力与锁行为测试（单写入者模型，未做并发压测）。
- **鉴权 / TLS / 限流 / 配额**等生产网关能力：本地合成服务，未实现也未测试。
- gas 预测、收益预测等“预测类”检查：模型显式不包含，无法执行、亦不在验收范围。

> `scripts/verify.py` 内置 `N/A` 通道：若某节因环境原因无法运行，会以
> “Checks not executed（不计为 pass）”单列并在汇总中打印，而不是伪装成通过。
> 当前环境运行结果为 42/42 PASS、0 N/A。

---

## 8. 项目目录

```
requirements.txt                 精确锁定依赖（pip freeze）
pyproject.toml                   pytest 配置
basefee_model/
  config.py errors.py cli.py
  encoding/  rlp.py crypto.py transaction.py merkle.py
  core/      fees.py validation.py state.py
  storage/   store.py
  replay/    replay.py events.py
  api/       schemas.py main.py
  fixtures/  __init__.py（确定性身份与负载画像）
data/canonical_fixture.json      可复用合成链夹具
tests/  test_*.py + conftest.py
        oracle/reference_oracle.py（不依赖被测内核）
        vectors/hand_vectors.json（手算基准）
scripts/verify.py                独立验证脚本
```
