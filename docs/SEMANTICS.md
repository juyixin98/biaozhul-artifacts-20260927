# 边界语义说明（Boundary Semantics）

本项目是一个**合成的、完全离线的** EIP-1559 式基础费用递推模型。本文档明确它
"是什么"与"不是什么"，任何未列出的能力都不应被假定存在。

## 1. 固定参数（不可在运行时改变）

来自 `config/protocol.json`，启动时加载为冻结对象 `basefee.params.PARAMS`：

| 参数 | 值 | 含义 |
|---|---|---|
| `chain_id` | `15590` | 合成链 ID，签名域的一部分 |
| `elasticity_multiplier` | `2` | target = gas_limit / 2 |
| `base_fee_max_change_denominator` | `8` | 单块费用最大变化 1/8 |
| `intrinsic_tx_gas` | `21000` | 单笔交易最小 gas |
| `genesis_base_fee` | `1_000_000_000` (1 gwei) | 创世块基础费用 |
| `genesis_gas_limit` | `30_000_000` | 区块 gas 上限，须被弹性倍数整除 |
| `min_base_fee` | `0` | 基础费用下限 |
| `max_fee_wei_limit` | `2²⁵⁶−1` | 费用字段算术上限（超过即 E032 溢出） |

修改其中任何一个都属于**协议升级**，必须同时提升 `protocol_version`
（当前 `synthetic-1559/1.0.0`），而不是在运行时改写。

## 2. 整数除法方向与最小增量

递推只依赖父块三元组 `(parent_base_fee, gas_used, gas_limit)`：

```
target = gas_limit // 2

gas_used > target（向上）:
    num   = parent_base_fee * (gas_used - target)
    delta = (num // target) // 8          # 两次连续向下取整（floor）
    若 delta == 0 且 parent_base_fee > 0: delta = 1   # 最小增量
    next  = parent_base_fee + delta

gas_used < target（向下）:
    num   = parent_base_fee * (target - gas_used)
    delta = (num // target) // 8          # 向下取整，无最小减量
    next  = max(min_base_fee, parent_base_fee - delta)

gas_used == target: next = parent_base_fee
```

* 所有除法都是**非负整数 floor 除法**（Python `//`），不使用浮点、不四舍五入。
  `tests/test_fee_recurrence.py::test_floor_division_direction_is_downward`
  专门钉死方向：`(100*3)//10//8 == 3`，任何"向上取整/四舍五入"实现都会得到 4。
* 向上路径有**最小 1 wei 增量**，但仅当父费用为正时成立；`parent_base_fee = 0`
  时不会凭空造出费用。向下路径**没有**最小减量，因此极小费用会"粘住"（sticky）。
* 满块接空块**不回到原点**（乘性步数的已知不对称性），测试中显式断言。

## 3. 交易扣费语义

```
priority_tip     = min(max_priority_fee_per_gas, max_fee_per_gas - base_fee)
effective_price  = base_fee + priority_tip
burned（销毁）    = base_fee      * gas_consumed
tip（付给 coinbase）= priority_tip * gas_consumed
sender_debit     = effective_price * gas_consumed + value
```

* `max_fee_per_gas < base_fee` ⇒ 拒绝，类别 `E020_MAX_FEE_BELOW_BASE`。
* `max_priority_fee_per_gas > max_fee_per_gas` 是**允许的**（EIP-1559 行为），
  实际小费自动截断为 `max_fee - base_fee`；接口另发 `W001` 警告。
* 任何费用乘积超过 `2²⁵⁶−1` ⇒ `E032_FEE_OVERFLOW`，不会静默回绕。
* gas 下限 `21000`、nonce 连续性、余额充足性分别有独立错误码（见
  `src/basefee/errors.py`）。
* 不执行 EVM：有效交易按其 `gas_limit` 全额消耗；出价高于实际价格的余量
  （`max_fee - effective_price`）留在发送者余额中，这正是费用守恒恒等式的来源。

## 4. 区块级规则（硬失败，整块原子拒绝）

* `gas_used > gas_limit` ⇒ `E040_BLOCK_GAS_EXCEEDED`；交易累计 gas 也不得超过上限。
* 区块头声明的 `gas_used` 必须等于所有**有效**交易 gas 之和，否则
  `E041_GAS_USED_MISMATCH`（无效交易被跳过时不占 gas）。
* `base_fee_per_gas` 必须等于父块递推出的费用，否则 `E043_BASE_FEE_MISMATCH`。
* 父块号/父哈希必须衔接（`E042`）；gas limit 必须为正且被弹性倍数整除（`E045`）。
* 默认为"跳过无效交易"模式（非严格）；`strict=true` 时任一交易无效即整块拒绝。
* 拒绝不改变世界状态（所有修改先在覆盖层上进行），也不写入 SQLite。

## 5. 密码学与标识域（重要差异）

本项目**不是**真实以太坊，为保持纯本地、无原生依赖：

* 哈希函数统一使用 **SHA-256 over RLP**，而非 keccak-256；
* 地址 = `SHA-256(pubkey_x || pubkey_y)` 的末 20 字节；
* 交易摘要 = `SHA-256(RLP(chain_id, nonce, max_fee, max_tip, gas, to, value, data))`；
* 签名是 secp256k1 + RFC 6979 确定性 ECDSA（成熟库 `ecdsa`），强制 **低 s**
  （拒绝高 s 可塑性，`E012`）；
* 恢复位 `v ∈ {0,1}` 采用 **ecdsa 库候选点列表下标**约定（已实证该库的候选顺序
  不等同于 y 奇偶，两个候选可能同为偶/奇；见 `encoding/crypto.py` 顶部说明）。

RLP 字段布局与 EIP-1559 type-2 交易一致；差异仅在哈希函数，且差异是全局、显式的。

## 6. 明确不做的事（不会被静默假定）

* **没有**任何真实链连接、P2P、RPC 上游或交易池；输入只能来自本地夹具/本地 HTTP。
* **没有**经济预测、gas 预言机、价格建议或 MEV 逻辑。
* **没有** EVM、合约执行、数据 gas（EIP-2028/4844）、退款、叔块/PoS 奖励。
* 费用销毁计入一个仅用于审计守恒的"销毁地址"余额；它不可被花费，也不代表真实链。
* 无法在本环境执行的检查（如真实链互操作）在报告中列入 `not_executed`，
  **不会**被写成已通过。
