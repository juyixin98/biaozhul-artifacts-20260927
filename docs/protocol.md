# 协议规范（字节级）— v1

本文件是**权威规范**。检测器（`ffg_slash`）与独立检查器（`independent/`）
分别独立实现本文件，二者不共享代码。所有整数为大端 uint64；所有十六进制
字段为小写 hex。

## 1. 域分离常量（32 字节）

```
DOMAIN_VOTE     = b"ffg-slash v1 vote"     || 0x00 * 15
DOMAIN_SNAPSHOT = b"ffg-slash v1 snapshot" || 0x00 * 11
```

`ffg-slash v1 vote` 为 17 字节，`ffg-slash v1 snapshot` 为 21 字节。

## 2. 投票体与消息根

投票体（80 字节）：

```
vote_body =
    u64(source_epoch) || source_root(32) ||
    u64(target_epoch) || target_root(32)
```

```
message_root = SHA256(vote_body)
```

合法投票要求 `source_epoch < target_epoch`。

## 3. 签名预映射（152 字节）与 Ed25519

```
signing_preimage =
    DOMAIN_VOTE(32) || u64(chain_id) || validator_pubkey(32) || vote_body(80)
signature = Ed25519.sign(signing_preimage)   // 64 字节
```

验签时 `validator_pubkey` 同时作为 Ed25519 公钥和预映射中的绑定字段，因此
无法用一个验证者的公钥偷换另一个验证者的签名。

## 4. 时代快照与快照根

快照成员按原始公钥字节升序排列：

```
snapshot_root = SHA256(
    DOMAIN_SNAPSHOT || u64(chain_id) || u64(epoch) ||
    u64(len(members)) ||
    concat_i( member_pubkey_i(32) || u64(weight_i) )
)
```

权重为正整数；某轮投票的成员资格与权重**只取 `target_epoch` 的快照**。

## 5. 投票信封（线上 JSON）

```json
{
  "chain_id": 4242,
  "validator_pubkey": "<32B hex>",
  "source_epoch": 1, "source_root": "<32B hex>",
  "target_epoch": 2, "target_root": "<32B hex>",
  "signature": "<64B hex>"
}
```

字段缺失、hex 非法、长度错误或类型错误（含 bool）一律为 `malformed`。

## 6. 冲突的精确定义

对同一 `validator_pubkey` 的两张**已验签**投票：

* **重复**：全部字段（含 signature）相同 → `duplicate`，非违规。
* **双签 double_vote**：`target_epoch` 相等且 `message_root` 不同。
* **包围 surround_vote**：存在严格区间包含
  `s1 < s2 < t2 < t1`（或交换 1/2 后成立）。相等端点不成立。
* 其余情况（例如不同目标轮次但不嵌套）不违规。

## 7. 最终性（明确简化规则）

* 某链接 `s -> t` 的投票权重满足 `3*weight >= 2*total(target_snapshot)` 时
  形成超级多数链接（严格 2/3，2/4 不够、3/4 够；每个验证者对同一链接只计一次）。
* 只有当链接源是当前 justified 检查点 **且** `t == justified.epoch + 1`
  （连续轮次）时，该链接才使 `t` 被 justified，并使源检查点被 finalized。
* 创世（epoch 0）默认 justified。非连续超级多数链接只记录为悬空链接，
  不触发 justified/finalized 变化。（这是有意的简化，真实 FFG 的分支选择与
  跨时代安全证明不在本地版范围内。）

## 8. 证据包（v1）

```json
{
  "version": 1,
  "type": "double_vote | surround_vote",
  "chain_id": 4242,
  "validator_pubkey": "<hex>",
  "vote_1": { ...信封... },
  "vote_2": { ...信封... },
  "vote_1_snapshot": {"epoch","chain_id","root","members":[{pubkey,weight}]},
  "vote_2_snapshot": { ... },
  "slashable_weight": {"epochs":[{"epoch","weight"}], "total_weight": n},
  "evidence_id": "<64 hex>"
}
```

`evidence_id` 是对除 `evidence_id` 外的以下字段做
`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=True)` 后
SHA256 的十六进制：`version, type, chain_id, validator_pubkey, vote_1,
vote_2, vote_1_snapshot, vote_2_snapshot`。

同一（验证者, 时代）只有一个可处罚权重值；双签两票同目标时代时权重不重复
相加；包围两票分属不同目标时代时分别取各自快照权重。

## 9. 独立复核步骤与判定

检查器顺序执行，任一失败立即返回对应原因（成功为 `valid`）：

1. `invalid_format` — 结构/字段/长度/轮次顺序非法，或不是 JSON
2. `unknown_type` — version 非 1，或 type 不在 {double_vote, surround_vote}
3. `bad_domain` — 内嵌 vote/snapshot 的 chain_id 或签名者与证据包不一致
4. `bad_signature` — 任一票 Ed25519 验签失败
5. 快照 epoch 必须等于该票 target_epoch，否则 `invalid_format`
6. `bad_snapshot_root` — 重算快照根与提交值不符（成员/权重被篡改）
7. `not_member` — 签名者不在其目标时代快照中
8. `rule_mismatch` — 两张票不满足声称的违规定义
9. `bad_evidence_id` — 重算 id 与包内不一致
