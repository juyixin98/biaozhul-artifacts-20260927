# 最小数据夹具清单

全部夹具由 `python -m tools.make_fixtures` **确定性生成**（固定 RFC6979 种子，
公开测试密钥，无真实资金/无生产账号）。重新运行得到逐字节相同的文件。

| 文件 | 内容 |
|---|---|
| `keys.json` | 4 把测试密钥 alice/bob/carol/dave 的种子、32 字节标量、压缩公钥 |
| `genesis.json` | 零输入铸币交易：21 个输出，每个 1000，各对应一个用例的锁定脚本 |
| `cases.json` | 21 个花费交易 + 每个用例的**独立预期分类**、判定理由、摘要（ecdsa 侧计算） |

**独立性**：密钥与签名由纯 Python `ecdsa` 库生成；被测核心使用 `cryptography`
（OpenSSL）验签。预期分类由 `make_fixtures.py` 中手工枚举的用例清单决定，
VM 不参与生成答案；工具仅在写盘前断言两侧摘要一致（防夹具写错，而非生成答案）。

## 用例矩阵

| ID | 标签 | 预期分类 | 覆盖点 |
|----|------|----------|--------|
| 00 | p2pk_ok | OK | P2PK 正常；正确域标签 |
| 01 | p2pkh_ok | OK | DUP/HASH160/EQUALVERIFY/CHECKSIG 完整 P2PKH |
| 02 | multisig_2of3_ab_ok | OK | M-of-N 满足；两把不同公钥各计一次 |
| 03 | multisig_2of3_only_one | STACK_UNDERFLOW | 签名数少于 m，取签时下溢 |
| 04 | multisig_1of2_boundary_ok | OK | 阈值边界 m=1 |
| 05 | multisig_duplicate_pubkey | SIG_DUPLICATED | **重复签名**，禁止同一公钥重复计数 |
| 06 | multisig_order_swap | THRESHOLD_NOT_MET | **顺序变化**：有效签名颠倒，有序匹配失败 |
| 07 | p2pk_wrong_domain | SIG_INVALID | **错误交易域**标签 |
| 08 | budget_exhausted | BUDGET_EXHAUSTED | **预算耗尽**：200 个 NOP 在验签前耗尽 200 步 |
| 09 | stack_underflow | STACK_UNDERFLOW | **栈下溢**：空解锁 + 锁首指令 DROP |
| 10 | hashlock_ok | OK | HASH160 原像 |
| 11 | hashlock_wrong_secret | EVAL_FALSE | 错误原像 |
| 12 | sha256_preimage_ok | OK | OP_SHA256 |
| 13 | hash256_preimage_ok | OK | OP_HASH256（双 SHA256） |
| 14 | ripemd160_preimage_ok | OK | OP_RIPEMD160（回退实现与 OpenSSL 交叉校验） |
| 15 | element_too_large | ELEMENT_TOO_LARGE | 256 字节元素 > 255 上限 |
| 16 | unknown_opcode | UNKNOWN_OPCODE | 白名单外字节 0x62 |
| 17 | unclean_stack | UNCLEAN_STACK | 结束留两个元素 |
| 18 | if_true_branch_ok | OK | IF 真分支；非活跃 ELSE 中的 RETURN 不执行 |
| 19 | if_false_branch_ok | OK | NOTIF 式假分支路径 |
| 20 | service_demo_ok | OK | 服务正常路径；重放/双花演示 |

每个用例在 `cases.json` 中含：`expected`（预期码）、`reason`（判定理由）、
`digest_hex`（ecdsa 侧独立摘要）、`prev_lock`（被花费锁）、`txid`、完整 `tx`。

## 失败类别与可区分性

- 输入错误：`UNKNOWN_OPCODE`、`ELEMENT_TOO_LARGE`、TX/REQUEST malformed
- 资源耗尽：`BUDGET_EXHAUSTED`（另有单元测试覆盖 STACK/DEPTH 类）
- 计算失败：`STACK_UNDERFLOW`、`EVAL_FALSE`、`SIG_INVALID`、
  `SIG_DUPLICATED`、`THRESHOLD_NOT_MET`、`UNCLEAN_STACK`
- 状态冲突：`UTXO_MISSING`、`TX_ALREADY_ACCEPTED`、`JOURNAL_CORRUPT`、
  `STATE_ROOT_MISMATCH`（后两类见 `tests/test_chain_and_replay.py`）
