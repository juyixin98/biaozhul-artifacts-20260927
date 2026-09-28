# 教学链：确定性小型状态机与 gas 计费

一个用于教学的、**完全确定性**的最小链执行环境：栈式虚拟机（64 位整数运算 +
键值读写 + 线性内存 + 嵌套调用）、显式 gas 计费、Ed25519 验签、SQLite 区块索引
和可重复的离线回放。

- **无宿主依赖**：执行路径不接触时钟、随机数、网络与文件系统。给定程序版本与
  输入摘要，任何进程重放得到逐字节相同的收据（测试在 `multiprocessing` spawn
  的全新解释器中验证）。
- **先扣费、后副作用**：每条指令先检查剩余 gas、扣除指令费与内存扩张费，然后才
  执行运算 / 内存 / 存储 / 调用。
- **状态与费用回滚范围分离**：执行失败回滚本帧及子帧的全部状态写入，但按规则
  保留费用（异常帧消耗其全部已分配 gas）。
- **结果绑定版本与输入**：每张收据含 `program_version` 与 `input_digest`
  （被签内容的 SHA-256），收据自身也有规范摘要。
- **本地合成夹具**：所有密钥都由固定种子确定性派生（`KeyPair.from_seed`），
  没有也不需要任何生产账号或真实业务数据。

## 目录结构

```
src/teaching_chain/
  config.py        # 配置（环境变量覆盖，默认本地路径）
  encoding.py      # 规范二进制编码、SHA-256、Ed25519 签名/验签（cryptography）
  vm/
    gas.py         # gas 价格表、intrinsic、内存扩张公式
    opcodes.py     # 操作码、立即数布局、字节码静态校验
    errors.py      # 稳定失败类别（收据 error_category）
    machine.py     # 执行引擎：扣费顺序、帧隔离、回滚边界
    assembler.py   # 助记符汇编/反汇编（教学辅助，不属共识核心）
  kernel.py        # 交易规整/验签、执行、收据、区块、纯内存链状态
  store.py         # SQLite 索引：区块/交易/收据（只追加、链尖指针、自检）
  node.py          # 内核 + 索引的服务装配、冷启动状态重建
  replay.py        # 离线回放：空状态重放 + 逐笔比对 + ACCEPT/REJECT/UNDETERMINED
  api.py           # FastAPI：提交/预演/查询，请求标识与诊断中间件
  diagnostics.py   # 结构化诊断（request_id、ACCEPT/REJECT 理由、敏感字段脱敏）
  cli.py           # keygen / assemble / serve
tests/             # 独立组织的测试（算术/gas/嵌套/回滚/确定性/内核/回放/API）
examples/          # 不依赖服务的 vm_demo.py 与端到端 client_example.py
```

模块有真实职责切分：编码验签、链状态内核、索引存储、离线回放各自独立，
不是单文件脚本，也不是只有接口的空工程。

## 本地启动

需要 Python 3.11+（开发于 3.12）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock          # 完整锁定版本
# 或：.venv/bin/pip install -e ".[test]"             # 按 pyproject 约束安装
.venv/bin/teaching-chain serve --host 127.0.0.1 --port 8000
# 等价于：.venv/bin/python -m teaching_chain.api
```

环境变量：`TEACHING_CHAIN_HOME`（默认 `./.teaching-chain`，SQLite 位于其下
`index.db`）、`TEACHING_CHAIN_HOST`、`TEACHING_CHAIN_PORT`。

运行测试：

```bash
.venv/bin/python -m pytest                          # 全部测试
.venv/bin/python -m pytest tests/test_gas_critical.py -v
```

## 快速体验（无需 HTTP）

```bash
.venv/bin/python examples/vm_demo.py
```

演示恰好耗尽 gas、少 1 gas 时 OOG 且副作用不发生、写后除零回滚、整数上界溢出、
嵌套 REVERT 的隔离回滚。

## HTTP 示例请求

先启动服务，另开一个终端：

```bash
# 端到端脚本：成功交易 + 写后除零 + 临界 gas 预演 + 篡改签名 + 查询
.venv/bin/python examples/client_example.py
```

手工请求（交易需要 Ed25519 签名，可用 `.venv/bin/teaching-chain keygen` 生成
合成密钥；实际签名请参照 `examples/client_example.py`）：

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool

# 只读预演（不封块）；可用 X-Request-ID 指定请求标识
curl -s -X POST http://127.0.0.1:8000/v1/dry-run \
  -H 'content-type: application/json' \
  -H 'X-Request-ID: demo-001' \
  -d '{"transaction": { ...已签名交易... }}'

# 提交一批交易并封块（执行失败的交易也会上链，status=0）
curl -s -X POST http://127.0.0.1:8000/v1/blocks \
  -H 'content-type: application/json' \
  -d '{"transactions": [ { ...}, { ...} ]}'

curl -s http://127.0.0.1:8000/v1/status
curl -s http://127.0.0.1:8000/v1/blocks/0
curl -s http://127.0.0.1:8000/v1/receipts/<tx_hash>
```

### 交易格式

待签内容（规范编码后签名）固定为四个字段：

```json
{ "chain": "teaching-chain-local", "nonce": 0, "code": "<hex>", "gas_limit": 100000 }
```

提交对象再附 `pubkey`（Ed25519 公钥 32 字节 hex）、`signature`（64 字节 hex）、
`signer`（`sha256(pubkey)` 前 20 字节的 hex）；可选 `"trace": true` 让收据
携带逐步轨迹。

## 助记符与字节码

```
PUSH8 <十进制有符号64位整数>   PUSH1 <0..255>
ADD SUB MUL DIV MOD ADDMOD MULMOD LT GT EQ ISZERO
POP DUP1 SWAP1
MLOAD MSTORE                   # 32 字节/字，栈顶为 offset（MSTORE 次顶为 value）
SLOAD SSTORE                   # SLOAD 栈顶 key；SSTORE 栈顶 key、次顶 value
CALL { <内联子字节码> }         # ULEB128 长度前缀 + 内联子码
REVERT STOP
```

```bash
printf 'PUSH8 7\nPUSH8 6\nMUL\nPUSH8 1\nSSTORE\nSTOP\n' \
  | .venv/bin/teaching-chain assemble
```

## Gas 规则（教学固定价格）

| 项目 | 费用 |
|---|---|
| STOP / JUMP / REVERT | 0 |
| PUSH / 算术 / 比较 / DUP / SWAP / 内存指令基础费 | 3 |
| POP | 2 |
| SLOAD / DIV / MOD / ADDMOD / MULMOD | 5 |
| SSTORE | 20 |
| CALL 基础费 | 10（无论子调用成败都收取） |
| 内存扩张 | 每字（32B）累计 `3w + ⌊w²/512⌋`，**只对新增字收增量** |
| 交易 intrinsic | `21 + Σ(零字节 1 / 非零字节 4)` |

- 指令执行顺序：解析立即数 → 检查并扣指令费 → 栈形状检查 → 需要内存时
  计算末端、检查并扣扩张费 → 才发生读写。
- 帧失败（OOG / 栈异常 / 溢出 / 除零 / REVERT / 超深）：该帧分得的 gas
  全部消耗、存储写入全部丢弃；CALL 成功才退还子帧未用 gas。
- intrinsic 超过 gas_limit：不执行，保留全部 gas_limit，收据 `OUT_OF_GAS`。
- 无效字节码（未知操作码 / 立即数截断）在静态校验阶段拒绝，`gas_used=0`。

## 失败类别（error_category）

`INVALID_BYTECODE`、`OUT_OF_GAS`、`STACK_UNDERFLOW`、`STACK_OVERFLOW`、
`INTEGER_OVERFLOW`、`DIV_BY_ZERO`、`INVALID_MEMORY`、`REVERTED`、
`CALL_DEPTH_EXCEEDED`（最大 CALL 深度 8）。

## 离线回放

```bash
.venv/bin/teaching-chain-replay --db .teaching-chain/index.db
# CI：存在 REJECT 时退出码为 2
.venv/bin/teaching-chain-replay --fail-on-reject --out replay-report.json
```

从索引库读取原始交易，在空 KV 状态上按区块顺序重新执行，逐笔比对存证收据：

- **ACCEPT**：收据摘要逐字节一致（版本、成败、gas、失败类别、状态根、返回值）；
- **REJECT**：关键字段任一不一致（存证被篡改或语义已变），报告列出具体差异；
- **UNDETERMINED**：存证 `program_version` 与当前节点不同，不做接受结论。

报告带 `report_digest`（报告体规范哈希）；`tests/test_store_replay.py`
在全新 spawn 进程中验证两次回放该摘要相同，并分别构造了状态根 / 费用 /
失败类别被篡改的 REJECT 用例。

## 诊断

每个请求可带 `X-Request-ID`（否则生成），响应头与体中均回显。服务端把
接受/拒绝/无法判定的理由以一行一条 JSON 写入 stderr，`signature`、`signer`、
`pubkey` 只留前缀与长度，`code` 只记录字节数与前几个字节，避免敏感数据完整落盘。

## 支持范围与关键取舍

- **单账户共享 KV**：教学 VM 不建模多账户 / 转账 / 余额；SSTORE 作用于一个
  全局键空间（键为非负 64 位整数，值为有符号 64 位整数）。
- **64 位有符号整数**：`ADD/SUB/MUL` 越界显式失败（不回绕）；`ADDMOD/MULMOD`
  使用 Python 全精度中间值再取模（模数必须为正，非正归为 `DIV_BY_ZERO`）。
- **CALL 是无参数沙箱**：子帧有独立空存储，成功不向父帧返回数据、不提交存储
  改动——它用于教学“嵌套失败与回滚边界/gas 传递”，不模拟合约间消息调用。
- **SSTORE 固定 20 gas，无退款 / 脏位定价**；内存扩张二次项是 EVM 风格的
  简化教学版。
- **只追加索引，不支持分叉回滚**；重复提交同一已上链交易会被去重拒绝。
- **批量静态失败即整批拒绝**（不封块）；执行失败交易正常入块（status=0）。
- **时间戳 / 随机源不可达**：诊断日志中的时间仅用于排障，绝不参与任何
  共识输出；`KeyPair.generate()` 只供本地工具使用，执行路径不调用。
- 非对称：静态校验失败（签名、形状、链号）不产生收据、不扣 gas；执行失败
  产生 status=0 收据并保留费用。

## 测试验收对应

| 验收要求 | 测试 |
|---|---|
| 临界 gas | `tests/test_gas_critical.py`（恰好成功 / 差 1 OOG / 扩张费不足 / 精确手算） |
| 嵌套调用失败 | `tests/test_vm_nested_calls.py`（子帧 OOG/REVERT/除零、传播、深度、退还） |
| 状态写后异常 | `tests/test_vm_state_rollback.py`（写后溢出/除零/REVERT 回滚、入参不被改） |
| 整数边界 | `tests/test_vm_arithmetic.py`（±2^63 边界、溢出、除零、全精度取模） |
| 不同进程重放相同收据 | `tests/test_determinism.py`（spawn 新进程比对执行结果与收据哈希）、`tests/test_store_replay.py`（跨进程报告摘要） |
| 状态与费用各自回滚范围 | 上述文件 + `tests/test_kernel.py`（失败笔状态根仅含前序成功笔、gas 全耗） |
| 断言具体结果与失败类别 | 全部断言字面期望值与 `error_category`，非“接口可调” |
| 参考答案独立于被测核心 | `tests/conftest.py` 内独立参考实现（手写 gas 常量、纯 Python 算术/费用函数），期望值不依赖从 VM 读回 |
| 篡改可检出 | `tests/test_store_replay.py`（状态根 / gas / 类别篡改 → REJECT；版本不同 → UNDETERMINED） |
