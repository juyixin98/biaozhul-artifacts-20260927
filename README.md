# StackVM — 受限栈脚本验证（本地测试交易）

> 教学/本地测试用途的**受限栈虚拟机**：实现常量、哈希运算、ECDSA 验签与 M-of-N
> 门槛脚本的验证，配合极简链状态内核、SQLite 索引存储与离线回放。
>
> **本项目不实现、也不声称兼容任何完整区块链协议**（不是 Bitcoin 主网/分叉的
> 实现）。仅支持下文明确列出的操作码子集，并存在有意为之的简化与分叉。

## 目录结构

| 路径 | 职责 |
|---|---|
| `stackvm/` | 编码与验签内核：操作码、脚本编解码、哈希、SECP256K1 验签、交易摘要、受限 VM |
| `chain/` | 链状态内核：UTXO 校验、预算执行结果、提交边界 |
| `chain/store.py` | SQLite 索引存储（UTXO 集、交易、链式写前日志） |
| `chain/replay.py` | 离线回放：从 genesis + 日志重建并复核状态根 |
| `service/` | FastAPI 服务（`/verify`、`/transactions`、查询接口） |
| `tools/` | 独立工具：`ecdsa` 库签名的夹具生成器、密钥生成、回放 CLI |
| `fixtures/` | 最小合成数据夹具（确定性生成，全部为测试密钥） |
| `tests/` | 独立测试套件（具体结果 + 失败类别断言、跨库交叉验证） |
| `config/default.toml` | 独立配置文件（限额、域标签、存储路径） |
| `examples/` | 服务调用示例（urllib 客户端、curl、真实会话记录） |
| `runlogs/` | 测试/服务运行日志（运行编号、中间栈状态、判定理由） |

## 唯一支持的操作码（白名单）

编号与 Bitcoin 历史编号保持一致（便于阅读），但语义为本文档定义的受限子集：

| 码 | 名称 | 语义 |
|---|---|---|
| `0x00` | `OP_0` / `OP_FALSE` | 压入空元素 |
| `0x01–0x4b` | 直接压入 | 后随 N 字节作为一个栈元素 |
| `0x4c/0x4d/0x4e` | `OP_PUSHDATA1/2/4` | 长度前缀压入（仍受元素大小上限约束） |
| `0x51–0x60` | `OP_1`..`OP_16` | 压入 1..16 的最小 ScriptNum 编码 |
| `0x61` | `OP_NOP` | 空操作（仍计预算） |
| `0x63/0x64/0x67/0x68` | `OP_IF/NOTIF/ELSE/ENDIF` | 受限条件分支（嵌套深度受限） |
| `0x69` | `OP_VERIFY` | 栈顶非真即失败 |
| `0x6a` | `OP_RETURN` | 执行到即失败（非活跃分支除外） |
| `0x6b/0x6c` | `OP_TOALTSTACK/FROMALTSTACK` | 备用栈搬运 |
| `0x75` | `OP_DROP` | 弹栈 |
| `0x76` | `OP_DUP` | 复制栈顶 |
| `0x7c` | `OP_SWAP` | 交换栈顶两项 |
| `0x82` | `OP_SIZE` | 压入栈顶元素字节长度 |
| `0x87/0x88` | `OP_EQUAL/EQUALVERIFY` | 字节级相等 |
| `0x93` | `OP_ADD` | 4 字节 ScriptNum 加法 |
| `0xa6` | `OP_RIPEMD160` | RIPEMD-160 |
| `0xa7` | `OP_SHA1` | SHA-1（完整性用途，非新脚本推荐） |
| `0xa8` | `OP_SHA256` | SHA-256 |
| `0xa9` | `OP_HASH160` | RIPEMD160(SHA256(x)) |
| `0xaa` | `OP_HASH256` | SHA256(SHA256(x)) |
| `0xab/0xac` | `OP_CHECKSIG/CHECKSIGVERIFY` | 压缩 SECP256K1 公钥 + DER 签名验签 |
| `0xae/0xaf` | `OP_CHECKMULTISIG(VERIFY)` | 有序贪心匹配的 M-of-N，禁止重复计数 |

**明确不支持**：任何其他字节（如 `0x62`、`0xab` 以外的密码操作码）一律在**解码期**
报 `UNKNOWN_OPCODE`；无 `OP_EVAL`、无脚本递归/动态求值；解锁脚本只允许压入操作。

与 Bitcoin 的**有意分叉**（不声明兼容）：
1. `OP_CHECKMULTISIG` **不要求** dummy 栈元素（无 Bitcoin 的历史 off-by-one）；
2. 裸 `OP_CHECKSIG/CHECKMULTISIG` 失败时**直接产生分类失败**，不压 0（保证失败可分类）；
3. 重复签名/同一公钥重复计数 → `SIG_DUPLICATED`，明确拒绝；
4. 执行结束要求**干净栈**（恰好一个为真的元素），否则 `UNCLEAN_STACK`；
5. 签名摘要为下文定义的单一域标签规范，无 sighash flag / code separator 规则。

## 快速开始

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # 精确复现见 requirements.lock

# 生成确定性合成夹具（使用与核心不同的 ecdsa 库签名）
python -m tools.make_fixtures

# 跑全部测试（结果写入 runlogs/test-runs/<TS>/）
pytest -q

# 离线回放已写日志的库
python -m tools.replay --db data/stackvm.db

# 启动服务
uvicorn service.app:app --reload
bash examples/curl_examples.sh
python examples/client_example.py
```

## 失败分类（四类可区分 + 成功）

- **输入错误 INPUT**：`REQUEST_MALFORMED`、`TX_MALFORMED`、`SCRIPT_MALFORMED`、
  `UNKNOWN_OPCODE`、`PUSH_ONLY_VIOLATION`、`ELEMENT_TOO_LARGE`
- **资源耗尽 RESOURCE**：`BUDGET_EXHAUSTED`、`STACK_TOO_LARGE`、`SCRIPT_TOO_LARGE`、
  `CONDITION_DEPTH_EXCEEDED`、`SCRIPT_DEPTH_EXCEEDED`
- **计算失败 COMPUTE**：`STACK_UNDERFLOW`、`EVAL_FALSE`、`UNCLEAN_STACK`、
  `SIG_INVALID`、`SIG_DUPLICATED`、`THRESHOLD_NOT_MET`、`MULTISIG_MALFORMED`、
  `INT_OVERFLOW`、`UNBALANCED_CONDITIONAL`、`VALUE_IMBALANCE`、`OP_RETURN_EXECUTED`
- **状态冲突 STATE**：`UTXO_MISSING`、`TX_ALREADY_ACCEPTED`、`JOURNAL_CORRUPT`、
  `STATE_ROOT_MISMATCH`

验证失败**只返回结构化失败分类，绝不执行任何转账**；仅当全部通过时 `submit` 才在
单个 SQLite 事务内花费旧 UTXO、登记新 UTXO 并追加链式日志。

## 交易摘要与域标签

待签名消息为 32 字节：`SHA256(canonical_json(sighash_doc))`，其中

```json
{"domain_tag":"STACKVM.SIGHASH/1","version":1,"locktime":0,
 "inputs":[{"txid":"<hex>","vout":0,"prev_script":"<hex>"}],
 "outputs":[{"value":1000,"script":"<hex>"}]}
```

`canonical_json` = `json.dumps(obj, sort_keys=True, separators=(",",":"),
ensure_ascii=False)`，hex 一律小写。摘要刻意不包含 `unlock`（解锁脚本携带签名，
入摘要会自引用；txid 仍覆盖 unlock，改动会使后续引用失效）。域标签不匹配（错误
交易域）或任何被签字段被改动，都会导致验签失败 `SIG_INVALID`。

## 限额（默认值，见 `config/default.toml`）

操作预算 200 步、栈元素 ≤255 字节、栈项 ≤100、脚本 ≤4096 字节、IF 嵌套 ≤16、
脚本上下文深度 ≤2（解锁段=1、锁定段=2，无动态求值）、M-of-N 中 N≤16。

## 复现文档

- `docs/REPRODUCE.md`：从零复现（含一次正常 + 异常运行的逐步骤）。
- `fixtures/MANIFEST.md`：每个夹具用例的独立预期与覆盖的失败类别。
- `runlogs/`：最近一次真实测试运行的编号、关键中间状态与判定理由。
