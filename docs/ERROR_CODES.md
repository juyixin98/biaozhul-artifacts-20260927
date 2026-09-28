# 错误码目录（Error Codes）

所有失败类别都是稳定字符串；测试断言类别而非仅"接口被调用"。

| 码 | 触发条件 | 层级 |
|---|---|---|
| `E001_HEX_DECODE` | 0x 十六进制无法解码/长度错 | 编码 |
| `E002_RLP_DECODE` | RLP 列表结构错、字段数不符 | 编码 |
| `E003_CANONICAL_ENCODING` | 非最短形式 RLP / 整数前导零 | 编码 |
| `E004_BAD_CHAIN_ID` | chain_id 与协议不符 | 编码/交易 |
| `E010_SIGNATURE_MALFORMED` | 无签名、v∉{0,1}、r/s 越界 | 签名 |
| `E011_SIGNATURE_INVALID` | 恢复出的点不匹配或 ECDSA 校验失败 | 签名 |
| `E012_SIGNATURE_HIGH_S` | s 不在低 s 区间（可塑性） | 签名 |
| `E013_SENDER_MISMATCH` | 声明发送者与恢复结果不符（保留） | 签名 |
| `E020_MAX_FEE_BELOW_BASE` | max_fee < 当前 base_fee | 费帽 |
| `E021_NEGATIVE_TIP` | 负小费（保留；实际由算术约束拦截） | 费帽 |
| `E022_BAD_FIELDS` | 字段缺失/为负/类型错/超 uint256 | 交易 |
| `E030_GAS_LIMIT_TOO_LOW` | gas < 21000 | 交易 |
| `E031_NONCE_MISMATCH` | nonce 与账户期望不符 | 交易 |
| `E032_FEE_OVERFLOW` | max_fee·gas 或加 value 超 uint256 | 交易 |
| `E033_INSUFFICIENT_BALANCE` | 余额不足支付 price·gas+value | 交易 |
| `E040_BLOCK_GAS_EXCEEDED` | gas_used>gas_limit 或累计超限 | 区块 |
| `E041_GAS_USED_MISMATCH` | 声明 gas_used ≠ 有效交易 gas 之和 | 区块 |
| `E042_BAD_PARENT` | 块号不连续或父哈希不衔接 | 区块 |
| `E043_BASE_FEE_MISMATCH` | 块 base_fee ≠ 父块递推值 | 区块 |
| `E044_BAD_BLOCK_HEADER` | 持久化被拒块等通用块头错误 | 区块 |
| `E045_GAS_LIMIT_INVALID` | gas_limit 非正或不被弹性倍数整除/翻倍越界 | 区块 |
| `E050_BLOCK_EXISTS` | 同号块已入库（HTTP 409） | 存储 |
| `E051_PARENT_UNKNOWN` | 父哈希与库内 head 不符 | 存储 |
| `E052_REPLAY_GAP` | 块号跳跃，未严格接在 head 后 | 存储 |
| `W001_PRIORITY_CAP_ABOVE_FEE_CAP` | （警告）小费帽高于费帽，已自动截断 | 非阻断 |
| `W002_MIXED_TX_ENCODING` | （警告）同块混用 structured 与 raw 交易 | 非阻断 |
