# 数据模型

## 交易（规范 JSON 字段即签名/哈希载荷）

| 字段 | 含义 |
|---|---|
| `type` | `transfer`（高度≥1）或 `mint`（仅创世块高度 0） |
| `nonce` | 发送方账户 nonce，transfer 必须严格 `上一 nonce + 1` |
| `sender` / `recipient` / `fee_recipient` | `rx1` + SHA-256(pubkey) 前 20 字节 |
| `amount` / `fee` | 十进制字符串（避免线上大整数/精度歧义），amount > 0，fee ≥ 0 |
| `txid` | `sha256(canonical_json(上述 7 字段))` |
| `pubkey` / `signature` | Ed25519 公钥（32B hex）与对上述 7 字段的签名 |

## 区块头

| 字段 | 含义 |
|---|---|
| `version` | 固定 1 |
| `height` | 0=创世（parent 为 64 个 0）；否则必须 = 父高度+1 |
| `parent` | 父区块恒等哈希 |
| `merkle_root` | 块内 txid 的 Bitcoin 式（尾节点复制）Merkle 根 |
| `difficulty` | 本块权重；必须属于共识允许集合 `{4,16}`；PoW 目标 `2^256 // difficulty` |
| `timestamp` | 非空字符串（合成链，不做强时钟检查） |
| `producer` | 出块者公钥；其地址必须在 `config/authorized_producers.json` |
| `nonce` | ASCII 十进制串；与其余头字段一起决定恒等哈希并满足 PoW |
| `pow_signature` | producer 对 32 字节恒等哈希的 Ed25519 签名 |

恒等哈希 = `sha256(canonical_json({version,height,parent,merkle_root,difficulty,timestamp,producer,nonce}))`。

## SQLite 表（storage/schema.py）

* `blocks`：所有密封有效块（活动链/分叉/被存档的失败候选），`is_active` 标记成员关系。
* `active_chain(height PK → block_hash)`：每高度恰一行，即“查询所见的完整版本”。
* `pending_blocks`：父未知的孤儿，按 `arrived_seq` 排序释放。
* `switch_plan(id=1)`：两阶段切换的持久计划（`PLANNED` 仅在事务内、提交态为 `DETACHED`）。
* `derived_events`：**可撤回派生索引**。列为
  `(seq, block_hash, height, txid, position_in_block, kind, address, amount_delta, nonce_delta)`，
  `kind ∈ {debit, credit, fee, nonce}`；`UNIQUE(block_hash, position_in_block)`。
* `account_state`：活动链的物化账户视图（balance, nonce），由事件增量维护、撤旧取逆。
* `tx_locations(txid, block_hash, height, on_active)`：同一 txid 的每次出现；
  切换翻转 `on_active`，因此“两条分支同一交易”在最佳链上恰好一个有效贡献。
* `diagnostics`：每次决策一行（请求标识、outcome、reason、tip/候选关键状态、时间）。
* `counters`：单调到达序号。

## 不变量（测试均有覆盖）

1. 活动链任意前缀都是父哈希相连的链（`active_chain` 与各块 `parent` 一致）。
2. `account_state` ≡ 对活动链 `derived_events` 求和；也 ≡ 全量重建结果。
3. 任意 txid 在 `tx_locations` 中可有多个出现，但 `on_active=1` 至多一个。
4. 切换后不存在“半新半旧”的可见状态（两阶段各自单事务）。
5. 越过最终性（将撤下确认数 > K 的块）的切换不发生任何状态变更，仅写拒绝诊断；
   候选块仍以 `is_active=0` 存档，便于审计。
