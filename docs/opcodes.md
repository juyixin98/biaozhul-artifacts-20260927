# 唯一支持的操作码

本系统是**受限栈脚本验证器（RSV）**，用于本地测试交易。它**不**实现、也**不**
声称兼容任何完整链的脚本系统（不支持时间锁、taproot、checksigadd、跨链操作码
等）。下表是穷举白名单：脚本中出现表外字节会在解析期直接拒绝
（`input.unknown_opcode` 或 `input.reserved_opcode`）。

编号沿用经典栈脚本的编号习惯仅为对照，语义以本文档为准。

## 常量 / 推送

| 字节 | 名称 | 语义 |
|---|---|---|
| `0x00` | OP_0 | 压入空字节串（脚本假值） |
| `0x01..0x4B` | 直接推送 | 后随 N 字节数据 |
| `0x4C` | OP_PUSHDATA1 | 1 字节长度前缀 + 数据（仅允许 N>0x4B） |
| `0x4D` | OP_PUSHDATA2 | 2 字节 LE 长度前缀（仅允许 N>0xFF） |
| `0x4E` | OP_PUSHDATA4 | 4 字节 LE 长度前缀（仅允许 N>0xFFFF） |
| `0x4F` | OP_1NEGATE | 压入 -1 |
| `0x51..0x60` | OP_1..OP_16 | 压入 1..16 |

推送必须使用最小编码：≤0x4B 字节的数据禁止使用 PUSHDATA\*，否则
`input.malformed_push`。

## 流控

| 字节 | 名称 | 受限语义 |
|---|---|---|
| `0x61` | OP_NOP | 空操作（计 1 步） |
| `0x63` | OP_IF | 栈顶为真则执行本分支 |
| `0x64` | OP_NOTIF | 栈顶为假则执行本分支 |
| `0x67` | OP_ELSE | 分支取反 |
| `0x68` | OP_ENDIF | 分支结束 |
| `0x69` | OP_VERIFY | 栈顶为假则 `compute.verify` |
| `0x6A` | OP_RETURN | **立即失败** `compute.op_return`（受限环境不允许数据载体） |

IF/ELSE/ENDIF 必须配对，嵌套深度 ≤ 配置上限（默认 8）。

## 栈操作

| 字节 | 名称 |
|---|---|
| `0x6B` | OP_TOALTSTACK |
| `0x6C` | OP_FROMALTSTACK（副栈空时 `compute.stack_underflow`） |
| `0x74` | OP_DEPTH |
| `0x75` | OP_DROP |
| `0x76` | OP_DUP |
| `0x7C` | OP_SWAP |

## 逻辑 / 比较

| 字节 | 名称 |
|---|---|
| `0x82` | OP_SIZE |
| `0x87` | OP_EQUAL |
| `0x88` | OP_EQUALVERIFY（不等即 `compute.equalverify`） |
| `0x91` | OP_NOT |
| `0x9A` | OP_BOOLAND |
| `0x9B` | OP_BOOLOR |

## 密码学

| 字节 | 名称 | 语义 |
|---|---|---|
| `0xA6` | OP_RIPEMD160 | RIPEMD-160 |
| `0xA8` | OP_SHA256 | SHA-256 |
| `0xA9` | OP_HASH160 | RIPEMD160(SHA256(x)) |
| `0xAA` | OP_HASH256 | SHA256(SHA256(x)) |
| `0xAC` | OP_CHECKSIG | ECDSA-secp256k1 验签 |
| `0xAD` | OP_CHECKSIGVERIFY | CHECKSIG 后要求真值 |
| `0xAE` | OP_CHECKMULTISIG | m-of-n 门槛验签 |
| `0xAF` | OP_CHECKMULTISIGVERIFY | CHECKMULTISIG 后要求真值 |

## 验签消息（与完整链不同的关键约定）

所有 CHECKSIG 校验的消息是：

```
message32 = SHA256(SHA256( "rsv-sighash-v1"
                         ‖ leb(len(network)) ‖ network
                         ‖ leb(len(domain))  ‖ domain
                         ‖ leb(len(cx))      ‖ cx ))
```

其中 `cx` 是交易的规范化序列化（**不含** witness/解锁脚本），`leb` 为
LEB128 长度前缀。签名绑定交易摘要**和域标签**：在 domain A 的摘要上签的名，
对 domain B 的交易必然验签失败。

**受限失败语义**：OP_CHECKSIG / OP_CHECKMULTISIG 验签失败时立即抛出
`compute.crypto.sig` / `compute.crypto.threshold`，而不是像完整链那样压 0
让脚本继续。这样每类失败都有确定、可断言的分类。

## m-of-n 栈布局与防重复计数

执行 CHECKMULTISIG 前栈（底→顶）：

```
sig_1 … sig_m  <m>  pub_1 … pub_n  <n>
```

- 要求 `1 ≤ m ≤ n ≤ 16`，m/n 使用最小脚本整数编码；
- 策略公钥不得重复（`compute.crypto.threshold_invalid`）；
- **同一签名字节串不得提供两次**——重复签名只可能对应同一公钥，
  禁止以此凑门槛（`compute.crypto.threshold_invalid`）；
- 匹配顺序无关：每个签名找第一个尚未使用且验签通过的公钥，
  每个公钥至多计一次；有效匹配数 < m 即 `compute.crypto.threshold`。
