# 边界语义说明 (UTXO 测试资产账本)

本文件定义模块边界、共识/校验语义、错误分类，以及**哪些检查在当前线格式下
无法通过端到端执行触发**（单列，禁止在结果中写成"已通过"）。

## 1. 模块边界与数据/错误契约

| 模块 | 职责 | 不做什么 |
| --- | --- | --- |
| `errors.py` | 四类错误分类 + 稳定 `code` + `to_dict()` 契约 | 不做判定 |
| `encoding.py` | 固定二进制布局、严格 JSON、txid/签名摘要/哈希链根 | 不做语义校验（金额范围等） |
| `crypto.py` | secp256k1 ECDSA(DER) 验签、确定性测试密钥 | 不决定"签什么"（摘要由编码层算） |
| `store.py` | SQLite 索引、花费审计、**单事务原子提交** | 不做共识规则判定 |
| `kernel.py` | 全部共识规则；产出 `BlockPlan` 后才允许提交 | 不直接写 sqlite（除通过 plan） |
| `journal.py` | run_id、逐步判定、状态快照、保全结论 | 不影响判定 |
| `replay.py` | 夹具装载 + 独立 oracle 对照 + 隔离重放 | 不自己生成"参考预期" |
| `fab.py` | 本地合成数据构造器（测试密钥/签名/组块） | 不保证产物合法（可造攻击变体） |
| `api.py` | FastAPI JSON 信封、串行化、错误码映射 | 不含规则 |

跨模块数据契约（不可变值对象）：`Outpoint / TxInput / TxOutput / Witness /
Transaction / BlockHeader / Block`（见 `encoding.py`）；存储读取契约 `Utxo`
（见 `store.py`）；写计划契约 `PlannedTx / BlockPlan`（见 `kernel.py`），
存储层只按 `committed_inputs / new_outputs / block` 这些只读属性工作。

## 2. 金额与守恒

* 金额、费用均为**整数**；单值范围 `[0, 2^63-1]`（`MAX_MONEY`），编码层 u64、
  语义层在 `[1, MAX_MONEY]`（输出）/`[0, MAX_MONEY]`（费用）内二次校验。
* **零值输出明确禁止**（`ZERO_VALUE`），先于验签检查。
* 常规交易守恒：`sum(inputs) == sum(outputs) + fee`，`fee >= 0`。
  求和使用有界累加，超过 `MAX_MONEY` 即 `AMOUNT_OVERFLOW`（溢出保护）。
* **发行**：只有高度 0 的 genesis 块允许无输入交易；genesis 交易不适用常规
  守恒（无中生有），且必须 `fee=0`。非 genesis 出现无输入交易即
  `ILLEGAL_ISSUE`。
* **费用在本测试资产中被销毁**，不分配给矿工、不进入币库交易——这是有意
  简化，使总量只由 genesis 发行决定。

## 3. 引用、双花与块内拓扑

* 每个输入引用 `Outpoint(txid, vout)`；被引用输出必须**当前未花费**。
* 三种状态由 `store.classify_outpoint` 区分：
  `unspent / spent / unknown`，分别导致成功、`DOUBLE_SPEND`、`UNKNOWN_OUTPOINT`。
* 块内暂定花费集合**同时**覆盖块内新输出与链上历史输出：同一枚链上 UTXO
  被块内两笔交易先后引用，第二笔报 `DOUBLE_SPEND`（`tx_index` 指向第二笔）。
* 单笔交易内两次引用同一 outpoint（"重复输入"）报 `DOUBLE_SPEND`（`tx_index`
  指向该交易）。
* 块内允许引用**前序**交易的输出（同块内链式交易）。
* **禁止前向引用**：交易引用块内排在其后（更大索引）的交易，报
  `FORWARD_REFERENCE`。
* **禁止引用环**：`kernel._check_topology` 用 Kahn 拓扑排序检测环
  （纯函数 `kernel._find_cycle_nodes` 可直接单测）。

### 3.1 关于"引用环"的不可执行性（重要）

在线格式中没有独立交易 id 字段：`txid = sha256(规范交易体)`，而交易体按
固定编码提交了该交易的全部输入 outpoint（其中包含它所引用的块内 txid）。

* **前向引用可表示**：被引用交易的 id 可由其自身 body 独立算出，引用者直接
  写入该 id（即使该交易排在后面）——因此 `FORWARD_REFERENCE` 能在端到端
  夹具中触发，已覆盖。
* **引用环在表示层不可构造**：环中每笔交易的 id 都（经其 body）提交环内下
  一笔交易的 id，等价于求一个哈希循环固定点 `x = H(...x...)`，在抗碰撞
  SHA-256 下不存在可行构造。长度为 2 的环（A 引用 B、B 引用 A）同样如此。

因此 `REFERENCE_CYCLE` 是**防御深度**：正常构造器（`fab.py`）无法产出会
触发它的字节流。它的正确性由对纯函数 `_find_cycle_nodes` 的单元测试覆盖
（无环 DAG、2 环、3 环、环+尾部），**不**在端到端回放夹具中声称"已执行
通过"。若未来引入独立的、非哈希提交的交易 id 字段，该检查即可被端到端
触发，本节应同步更新。

## 4. 签名

* 曲线 secp256k1；公钥仅接受 33 字节 SEC1 压缩点；签名为 ECDSA DER。
* 签名消息固定为 `sha256(b"utxo-ledger/sighash/v1" + 无见证规范交易体)`。
  见证不参与签名摘要（否则签名无法覆盖自身），但完整交易字节与
  `witness_root` 绑定见证，块头 `witness_root` 不一致即 `ROOT_MISMATCH`。
* 每个输入必须由**被花费输出的公钥**对应的私钥签名；见证数必须等于输入数。
* ECDSA nonce 由成熟库随机生成（该库未暴露 RFC6979 确定性 k）。"固定编码"
  指消息摘要的构造固定；夹具把签名字节固化在 JSON 中，回放读文件而非重新
  签名，因此整体可复现。
* 任何验签失败（篡改、错误持有者、DER 非法）统一为 `COMPUTATION_FAILED /
  SIGNATURE_INVALID`，与输入错误、状态冲突明确区分。

## 5. 整块原子性

* 内核在内存暂定视图中逐笔规划；**任何**一笔非法即抛错，不产生 `BlockPlan`。
* `SqliteStore.apply_block` 在单个 `BEGIN IMMEDIATE ... COMMIT` 中完成删除
  被花费 UTXO、插入新 UTXO、写审计/块/交易索引/meta；任何异常 ROLLBACK。
* 验收要求"失败后所有 UTXO 保持原样"由两层保证并有断言：
  规划失败不调用 apply；apply 内部兜底冲突也回滚。测试与回放对每次拒绝做
  前后快照比对（height/tip/utxo_count/utxo_root/block_count）。

## 6. 错误分类（四类，可机器区分）

| 类别 | 含义 | 主要 code | HTTP |
| --- | --- | --- | --- |
| `INPUT_ERROR` | 与状态无关的输入缺陷，改输入可重试 | MALFORMED_ENCODING, ZERO_VALUE, AMOUNT_OUT_OF_RANGE, AMOUNT_OVERFLOW, WITNESS_COUNT_MISMATCH, TXID_DUPLICATE, ILLEGAL_ISSUE, INVALID_FEE, CONSERVATION_MISMATCH, BAD_GENESIS | 400 |
| `STATE_CONFLICT` | 与已提交/暂定状态冲突 | DOUBLE_SPEND, UNKNOWN_OUTPOINT, FORWARD_REFERENCE, REFERENCE_CYCLE, BLOCK_CONFLICT | 409 |
| `RESOURCE_EXHAUSTED` | 触及资源/预算上限 | RESOURCE_LIMIT（details.limit_name 区分） | 422 |
| `COMPUTATION_FAILED` | 确定计算/验证失败、底层故障 | SIGNATURE_INVALID, HASH_MISMATCH, ROOT_MISMATCH, STORAGE_FAILURE, INTERNAL_ERROR | 422 / 500 |

分类设计回应需求中的四象限：

* **输入错误** → `INPUT_ERROR`（零值、守恒、见证数、非法发行……）
* **状态冲突** → `STATE_CONFLICT`（双花、未知 outpoint、前向/环、块定位）
* **资源耗尽** → `RESOURCE_EXHAUSTED`（块字节、交易/输入/输出条数）
* **计算失败** → `COMPUTATION_FAILED`（验签、摘要/根不一致、存储/内部错误）

## 7. 未做 / 不适用的检查（不声称已通过）

* **引用环的端到端触发**：见 §3.1，线格式不可构造；仅纯函数单测覆盖。
* **时间戳/难度/POW/最长链重组**：测试资产单线性链，不做时钟共识与分叉
  选择；`timestamp` 仅固定编码，不校验漂移。
* **P2P/内存池/并发出块者**：API 用进程内锁串行化提交，不模拟网络竞争。
* **费用回收/矿工币库**：费用销毁（§2）。
* **脚本系统**：支付授权固定为"secp256k1 压缩公钥 + ECDSA"，无图灵脚本。

## 8. 复现实验

```bash
./verify.sh
# 或分步
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python scripts/gen_fixtures.py
PYTHONPATH=src .venv/bin/python -m pytest tests/
PYTHONPATH=src .venv/bin/python -m utxo_ledger.replay fixtures/validation_cases.json
```

每条用例的 `logs/<run_id>.jsonl` 含运行编号、逐笔判定、失败类别/code、
提交前后状态快照与 `state_preserved_on_failure` 结论，可凭 run_id 重放定位。
