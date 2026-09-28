# 测试资产 UTXO 账本 (Test-asset UTXO ledger)

一个从零实现的、用于**测试**的 UTXO 账本：支持输入引用、输出、交易费、多交易
区块验证；金额为整数；Ed25519 固定编码验签；整块原子提交（任一交易非法则整块
不提交）。所有外部参与方均为**本地确定性合成夹具**，无生产账号、无真实业务数据、
无网络依赖（密码学使用成熟的 `cryptography` 库）。

技术栈：Python 3.10+ · FastAPI · SQLite（标准库 `sqlite3`）· `cryptography`（Ed25519）。

---

## 1. 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 或 -r requirements.lock.txt

# 一键验证：离线夹具回放 + 链重建 + 全部 pytest（产物在 logs/）
./scripts/verify.sh

# 单独运行
.venv/bin/python -m pytest                          # 全部单元/集成测试
.venv/bin/python -m utxo_ledger.replay fixtures \
    --file fixtures/cases.jsonl --logdir logs       # 离线回放固定夹具
.venv/bin/python -m utxo_ledger.api                 # 启动 HTTP 服务（127.0.0.1:8080）
```

固定夹具可重新生成（生成时会用独立 oracle 交叉核对期望值）：

```bash
.venv/bin/python scripts/build_fixtures.py
```

---

## 2. 模块边界与数据/错误契约

代码**不是**单文件堆叠，也没有空壳模块；每个模块有明确职责与契约：

| 模块 | 职责 | 边界契约 |
|---|---|---|
| `utxo_ledger/protocol.py` | 冻结的协议常量（域分隔标签、尺寸、限额、补贴） | 唯一事实源；**独立 oracle 不导入它**（在 `tests/oracle.py` 中按规格文本重新声明） |
| `utxo_ledger/errors.py` | `ErrorCode`、`ErrorCategory`、`LedgerError` | 跨模块只抛 `LedgerError`；四类错误可区分（见 §5）；码→类别映射是全射（测试保证） |
| `utxo_ledger/crypto.py` | Ed25519 签名/验签（仅调用成熟库） | 畸形材料→`input/*`；签名本身不通过→`state/sig_tampered`；库故障→`computation/*` |
| `utxo_ledger/encoding.py` | 固定二进制编解码、txid、sighash、merkle、区块哈希；**仅结构校验** | 解码消费且仅消费全部字节；语义规则不在此层 |
| `utxo_ledger/kernel.py` | 纯链状态验证（无 I/O） | 输入 `Block`+只读 `ChainView`，成功返回不可变 `BlockEffect`，失败抛错且**不触碰可提交状态** |
| `utxo_ledger/storage.py` | SQLite 索引（UTXO / 区块 / 交易 / 花费历史） | 实现 `ChainView`；唯一写入口 `apply_block`，单条 `BEGIN IMMEDIATE` 事务，失败整体回滚 |
| `utxo_ledger/node.py` | 编排：解码→内核验证→原子提交 + 日志 | 返回 `BlockAttemptResult`，含提交前后 UTXO 指纹，便于断言“失败后无变化” |
| `utxo_ledger/runlog.py` | 结构化 JSONL 运行日志 | 稳定 `run_id`、逐 tx 判定、提交前后 UTXO 指纹与 SHA-256 |
| `utxo_ledger/replay.py` | 离线回放（库 + CLI）：`chain` / `fixtures` | 与线上同一条 `submit_raw_block` 路径；每用例独立磁盘库 |
| `utxo_ledger/api.py` | FastAPI HTTP 边界 | 只做 hex/JSON 传输与状态码映射，不实现任何链规则 |

内核与存储之间的**数据契约**：内核返回 `BlockEffect`（含每笔 `TxEffect` 的
`spent`/`created`/`fee`），存储据此在单事务内删除/新增 UTXO、追加花费历史、推进
tip。内核不依赖 SQLite，存储可被内存实现替换（`kernel.InMemoryChainView`）。

---

## 3. 线格式与哈希（固定编码）

全部小端、长度前缀向量，域分隔标签绝不复用：

- 交易：`TX_TAG | ver u32 | n_in u16 | n_out u16 | inputs | outputs`
  - input：`txid(32) | vout u32 | sig_len u16 | sig`
  - output：`value u64 | pk_len u16 | pk(32)`
- `txid = SHA256(TX_TAG ‖ 完整交易体)`（**提交包含签名字段**，改签名即改 txid）。
- `sighash = SHA256(SIGHASH_TAG ‖ TX_TAG ‖ 不含签名字段的交易体)`：一笔交易一个
  共享摘要，每个输入须用“被花费输出”的公钥对该摘要签名。
- 区块：`BLOCK_TAG | ver u32 | height u64 | prev_hash(32) | n_tx u32 | tx...`
- 区块哈希 = `SHA256(BLOCK_TAG | ver | height | prev_hash | merkle_root | n_tx)`；
  merkle 根按顺序提交全部 txid（奇数层复制最后一个元素）。

结构解码要求精确消费：多余尾随字节 → `invalid_encoding`。

---

## 4. 语义边界（重点）

- **未花费性**：引用的输出必须 (a) 已提交且未花费，或 (b) 由本区块中**更早**的
  交易创建。
- **顺序**：允许引用前序交易；引用同区块中更靠后的交易 → `forward_reference`。
- **环**：严格向后引用使多交易环无法拼装；唯一剩余的环形态是“交易引用自身
  输出”，它要求输入 txid 等于自身 txid，即 SHA-256 原像不动点，**在编码层不可
  构造**。内核仍保留同位（self）分支 `cyclic_reference` 作为纵深防御，并由白盒
  测试直接覆盖（见 `tests/test_kernel.py::test_cyclic_reference_guard_whitebox`）。
- **双花区分**：
  - 同一交易输入向量里同一 outpoint 出现两次 → `duplicate_input`；
  - 同一区块中两笔不同交易花费同一 outpoint → `double_spend`；
  - 花费“已提交且已花费”的输出 → `already_spent`；
  - 引用从不存在的输出 → `unknown_outpoint`。
- **金额与守恒**：金额为非负整数、`≤ 2^64−1`；任何求和都走 checked u64 加法，
  溢出 → `value_overflow`（不回绕）。普通交易要求 `Σ输入 ≥ Σ输出`，差额即费；
  零值输出 → `zero_value_output`。
- **币基**：仅区块第 0 笔，一个 null 输入（`vout = height`，无签名）；其输出总额
  必须**恰好**等于 `BLOCK_SUBSIDY(1_000_000) + 本块总费用`，否则 `bad_coinbase`。
  币基输出在本块内即可被后续交易引用。
- **验签**：每个输入用被花费输出记录的公钥，对该交易的固定 sighash 做 Ed25519
  验证；篡改消息字节、翻转签名位、用错密钥均 → `sig_tampered`。
- **原子性**：内核无写表面；存储在单事务内应用整个 `BlockEffect`。任一步失败，
  事务回滚，UTXO 集合与失败前**逐字节一致**（测试用失败前后快照指纹 + 重开数据库
  文件双重断言）。

---

## 5. 四类错误（可区分，日志中带 `category`）

| category | 含义 | 典型 code | HTTP |
|---|---|---|---|
| `input` | 调用方输入/编码/材料畸形 | `malformed`、`invalid_encoding`、`bad_signature`、`zero_value_output`、`not_found` | 400 |
| `state` | 与已提交链状态/语义冲突 | `unknown_outpoint`、`already_spent`、`duplicate_input`、`double_spend`、`forward_reference`、`cyclic_reference`、`fee_negative`、`value_overflow`、`bad_coinbase`、`sig_tampered`、高度/prev_hash 不符 | 409 |
| `resource` | 资源/限额耗尽 | `block_too_large`、`too_many_txs`、`too_many_inputs/outputs`、`too_many_sigops`、`block_validation_steps_exceeded` | 422 |
| `computation` | 机制/库/序列化故障 | `crypto_failure`、`storage_failure`、`serialization_failure`、`internal_error` | 500 |

---

## 6. 测试如何验证（不只“接口能调用”）

- **断言具体结果与失败类别**：每个负例都断言精确 `ErrorCode`、`category`，必要时
  断言 `tx_index`。
- **逐交易参考状态对照**：`tests/oracle.py` 是**独立参考实现**——不导入任何生产
  模块（`encoding/kernel/storage` 均禁止），仅用标准库 + 同一个成熟密码库，常量
  按规格文本重新声明。正常路径上，对“币基..第 k 笔”的**每个前缀**，分别用
  生产栈（全新 SQLite）和独立 oracle（全新 `OracleState`）独立回放并逐 UTXO 比较。
- **四个指定负例**：区块内双花、重复输入、零值输出、签名篡改全部覆盖；失败后断言
  生产 UTXO 集合未变且等于未被改动的 oracle 状态。
- **参考答案不由被测核心生成**：`fixtures/cases.jsonl` 里的期望码/类别在
  `scripts/build_fixtures.py` 生成时用独立 oracle 再核对一遍。
- **运行日志可重放**：每次运行有稳定 `run_id`（UTC 时间+随机后缀），JSONL 记录
  区块大小、txid、逐 tx 判定理由、费用、提交前后 UTXO 集合计数与 SHA-256；失败用例
  的磁盘数据库保留在 `logs/<run_id>-dbs/` 供排查。

运行：

```text
43 passed
```

覆盖：编解码往返/尾随字节/坏标签/merkle 顺序、Ed25519 各类失效、四类必测负例、
前向引用、守恒、未知/已花费输出、币基超额认领、u64 溢出、资源限额、SQLite 索引/
花费历史/重开文件原子性、HTTP 状态码映射、链重建 tip 一致。

---

## 7. 离线回放

- `fixtures`：对自描述 JSONL 用例逐条在**全新磁盘库**上回放；校验接受/拒绝、
  精确 code/category、拒绝时 `state_unchanged`，以及 `fee_total`。
- `chain`：把源库区块日志按高度重放进全新库，逐块比对哈希并比对重建 tip，证明仅
  靠区块日志即可重建状态。

---

## 8. 明确“未执行 / 不冒充通过”的检查

以下属于真实系统通常需要、但**本测试资产刻意不做或无法在此环境执行**的项，单列
以免被误读为“已通过”：

1. **真正的自引用环（self-spend）不可在线编码**：它等价于求 SHA-256 原像不动点。
   因此没有也不可能有“构造出自引用交易并被拒绝”的端到端用例；`cyclic_reference`
   分支由白盒单测覆盖。
2. **工作量证明 / 难度 / 时间戳**：测试资产不需要挖矿，未实现也未验证。
3. **真实网络 / P2P / 并发多写者冲突**：单进程单连接；`check_same_thread=False`
   配合 `BEGIN IMMEDIATE` 串行化写入，但未在多进程/多机下压测。
4. **性能/拒绝服务压测**：仅验证限额会被触发（如 `too_many_txs`），未做大体积
   基准或耗时断言。
5. **密码库版本供应链证明**：固定了 `cryptography==43.0.3`，但未在离线环境核验其
   wheel 签名/哈希；`requirements.lock.txt` 仅锁定版本，不等同于签名校验。
6. **持久化备份/恢复、WAL 归档、崩溃中途恢复**：有单事务回滚与重开文件验证，未做
   进程 kill -9 / 断电注入。
7. **恒定时间/侧信道**：验签正确性依赖成熟库，未做侧信道评估。

---

## 9. 目录

```text
utxo_ledger/      生产包（协议/错误/密码/编码/内核/存储/节点/日志/回放/API）
tests/            pytest 测试 + 独立 oracle + 合成夹具 + 场景构造
fixtures/         固定 JSONL 用例（可由脚本重新生成）
scripts/          verify.sh / build_fixtures.py / make_chain_db.py
logs/             运行日志、报告、演示与重建数据库（运行 verify.sh 后产生）
requirements.txt  直接依赖固定版本；requirements.lock.txt 为全传递锁定
```
