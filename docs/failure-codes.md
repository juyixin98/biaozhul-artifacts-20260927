# 失败分类码契约

任何验证失败都只返回**验证失败分类**，绝不执行转账。四大顶层类别彼此可区分：

| 类别 | 含义 | 触发层 |
|---|---|---|
| `input` | 输入错误：结构/编码不合法 | 解析、交易反序列化、密钥结构校验 |
| `state` | 状态冲突：链状态前置检查不通过 | 内核（UTXO/双花/域/金额） |
| `resource` | 资源耗尽：元素/栈/步数/深度超预算 | 解析器与栈机 |
| `compute` | 计算失败：脚本执行期判定不通过 | 栈机、验签 |

## input

| 码 | 含义 |
|---|---|
| `input.malformed_tx` | 交易结构不合法（缺字段、类型错、金额越界、输入/输出数越界） |
| `input.malformed_script` | 脚本编码不合法（保留位之外的通用编码问题） |
| `input.malformed_push` | 推送长度与实际不符，或非最小推送编码 |
| `input.crypto.pubkey` | 公钥不是合法 SEC1 secp256k1 编码 |
| `input.crypto.sig_encoding` | 签名不是合法 DER / r,s 越界 |
| `input.unknown_opcode` | 操作码不在唯一白名单中 |
| `input.reserved_opcode` | 经典编号中保留/禁用的字节 |
| `input.script_too_large` | 脚本原始字节超过 2048B |
| `input.domain_missing` | 交易缺少域标签 |

## state

| 码 | 含义 |
|---|---|
| `state.unknown_outpoint` | 引用的 UTXO 不存在 |
| `state.already_spent` | UTXO 已花费（含同笔交易内重复引用同一 outpoint） |
| `state.domain_conflict` | 交易域标签与被花 UTXO 所在域不一致（**错误交易域**在此拦截） |
| `state.imbalance` | 输入金额之和 ≠ 输出金额之和 |
| `state.bootstrap_conflict` | 创世纪重复初始化且内容冲突 |

## resource

| 码 | 含义 | 默认预算 |
|---|---|---|
| `resource.element_too_large` | 单个栈元素超限 | 520B |
| `resource.stack_overflow` | 栈元素个数超限 | 64 |
| `resource.op_budget_exhausted` | 操作步数超限 | 128 条指令 |
| `resource.script_depth_exceeded` | 嵌套分支深度超限 | 8 |

## compute

| 码 | 含义 |
|---|---|
| `compute.stack_underflow` | 取数时栈元素不足 |
| `compute.unbalanced_if` | IF/ELSE/ENDIF 不配对 |
| `compute.verify` | OP_VERIFY 栈顶为假 |
| `compute.op_return` | 执行到 OP_RETURN |
| `compute.equalverify` | OP_EQUALVERIFY 两元素不等（含哈希原像错误、P2PKH 公钥错误） |
| `compute.crypto.sig` | 验签失败（**错误交易域的签名**也落此码，状态层另以 domain_conflict 先行拦截） |
| `compute.crypto.threshold` | CHECKMULTISIG 有效签名数未达 m-of-n |
| `compute.crypto.threshold_invalid` | m/n 非法、策略公钥重复、提供的签名重复 |
| `compute.script_false` | 执行结束栈顶为假 |
| `compute.script_empty` | 执行结束栈为空 |
| `compute.dirty_stack` | clean-stack：结束后栈上残留多于一个元素 |
| `compute.internal` | 成熟密码库内部异常（与“签名为假”区分） |

## 失败处理顺序（短路）

结构（input）→ 状态前置（state）→ 金额守恒（state）→ 逐输入脚本
（resource / compute）。只有全部通过且 `submit` 时，才在**单个 SQLite
事务**内更新 UTXO；任何失败路径都不触碰状态。测试
`test_rejected_tx_changes_nothing` 对每条失败交易逐字节核对状态根不变。
