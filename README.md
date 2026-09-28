# teachchain — 教学链：确定性状态机与 gas 计费

一个**刻意做小、完全确定性**的教学用链内核：u64 栈式虚拟机、整数运算、
键值存储读写、嵌套调用、显式 gas 计费、Ed25519 验签、SQLite 索引存储，
以及“只信任签名输入”的离线回放核验。所有账户、密钥、数据均为**本地合成
夹具**，不依赖任何真实业务系统或生产账号。

> 不是生产区块链。没有网络共识、没有真实价值、没有 Gas 市场；目标是让
> “确定性执行 / 回滚边界 / 费用保留 / 结果绑定版本与输入摘要”这些机制
> 可以被**独立重放与逐字段核验**。

---

## 1. 架构与模块职责

```
src/teachchain/
├── version.py       # 程序（内核）版本：每次执行结果都绑定它
├── errors.py        # 三类失败：Rejected / Halt / Revert + ReplayMismatch
├── gas.py           # 价格表 v1：内在费用、操作码费用、内存扩张公式、SSTORE 退款
├── opcodes.py       # 操作码定义、字节码解码、教学汇编器（支持标号）
├── vm.py            # 确定性虚拟机：栈/内存/存储/嵌套调用帧（不接触时间随机源）
├── crypto.py        # Ed25519 签名、SHA-256 摘要、公钥派生地址、规范 JSON
├── models.py        # 交易体/签名信封的规范化校验与验签准入
├── kernel.py        # 链状态内核：准入、托管扣费、执行、写集提交、收据与状态根
├── storage.py       # SQLite 索引存储：信封/收据/合约/账户/存储/注资流水
├── replay.py        # 离线回放：新建干净内核，只信任信封，逐字段核对
├── service.py       # 内核 + 索引存储的装配与启动重建
├── diagnostics.py   # 结构化诊断（带 request_id，敏感字段脱敏）
├── config.py        # 环境变量配置
├── fixtures.py      # 固定种子的本地合成密钥/账户/示例合约/签名信封
├── api.py           # FastAPI 适配层（HTTP 只做 I/O，不含执行语义）
└── cli.py           # 本地命令：demo / serve / replay / keygen / asm

tests/               # 独立测试（期望值手工推导并硬编码，不由被测实现生成）
└── helpers/         # 跨进程重放的“进程 A”链构建脚本
examples/            # 已签名的示例交易（可直接 POST）
```

设计约束：**执行语义只存在于 `vm.py` + `kernel.py`**；API、CLI 都是薄壳。
SQLite 里的账户/存储表是可重建的**索引视图**，权威输入是签名过的交易信封。

---

## 2. 行为约定如何落实

1. **每个操作及内存扩张费用明确，先检查剩余 gas 再执行副作用。**
   所有费用在 `gas.py` 常量化；VM 在每条指令执行前先扣费，
   不足即 `out_of_gas`（见 `vm.py::_charge`，在任何写操作之前调用）。
   内存按字扩张，二次计价 `cost(w)=3w + w²/512`。
2. **执行失败回滚状态，但按规则保留费用。**
   - `Revert`（程序主动）：帧内写集丢弃，**剩余 gas 退还**；
   - 异常中止（`out_of_gas`/`invalid_instruction`/`stack_*`…）：
     帧内写集丢弃，**已转发 gas 全部消耗**；
   - 准入拒绝（`Rejected`）：不执行、不入块、不扣费、nonce 不递增。
   嵌套 `CALL` 的回滚边界就是“帧”：子帧失败不影响父帧已做的写。
3. **宿主时间/随机数不可访问。**
   `vm.py/gas.py/kernel.py/opcodes.py/models.py/replay.py` 等被
   `tests/test_determinism_contracts.py` 用 AST 静态扫描，禁止导入
   `time/random/secrets/datetime/uuid/...`；请求 ID 由宿主层注入，
   未注入时用占位符 `-`。唯一使用密码学随机源的是 `cli keygen`。
4. **执行结果绑定程序版本及输入摘要。**
   交易摘要 = SHA-256(规范化 JSON 交易体)；收据包含 `engine_version`、
   `input_digest`(=tx_hash)、执行前/后 `state_root` 与整体 `result_digest`。
   回放时版本或任一字段不一致都会判 `ReplayMismatch`。

---

## 3. 指令集（u64 栈机，栈序遵循 EVM 习惯）

所有二元指令“**先弹出的栈顶是第一操作数 a**，计算 `a op b`”。
例如压入顺序为先 `b` 后 `a`：`PUSH 3 PUSH 17 DIV` = 17/3 = 5。

| 指令 | 栈（栈顶→下） | 语义 | gas |
|---|---|---|---|
| STOP | — | 成功结束 | 0 |
| ADD/SUB/MUL | a,b | `(a±×b) mod 2^64` | 5 |
| DIV/MOD | a,b | 商/余；**b=0 得 0**（不崩溃） | 5 |
| LT/GT/EQ | a,b | `a<b / a>b / a==b` → 0/1 | 3 |
| ISZERO | a | `a==0` → 0/1 | 3 |
| PUSH imm8 | — | 压入 8 字节大端立即数 | 3 |
| POP/DUP n/SWAP n | | 栈操作（n=1..16，上限 1024） | 2/3/3 |
| JUMP/JUMPI | dest / dest,cond | 只能跳到 JUMPDEST 字节偏移 | 8/10 |
| JUMPDEST | — | 跳转落点 | 1 |
| CALLDATALOAD/CALLDATASIZE | i / — | 读输入字（越界为 0）/ 字数 | 3/2 |
| MLOAD/MSTORE | off / off,val | 按字读写内存（惰性扩张） | 3 |
| MSIZE | — | 当前内存字数 | 2 |
| SLOAD/SSTORE | key / key,val | 账户键值存储读/写 | 800 / 动态 |
| CALL | gas,slot,addr | 嵌套调用（63/64 转发，深度≤32） | 700 |
| RETURN/REVERT | off,len | 成功返回 / 主动回滚（带输出） | 0 |
| INVALID (0xFE) | — | 异常中止 | 0（全损） |

整数：加减乘模 `2^64` 回绕；除以 0 / 对 0 取模得 0。
SSTORE 计费：`0→x` 20000（set）、`x→y≠0` 5000（reset）、同值 200（noop）；
`x→0` 给 15000 原始退款，结算时截断为“执行已耗 gas 的 1/2”。

汇编器支持标号：`label:` 绑定其后指令的**字节偏移**，`PUSH label JUMP` 跳转。

### 失败类别（收据 `halt_code`）

`revert`（主动回滚，退剩余 gas）、`out_of_gas`、`invalid_instruction`、
`invalid_jump`、`stack_underflow`、`stack_overflow`、`invalid_return_range`。
准入层另有 `bad_signature`/`sender_mismatch`/`bad_nonce`/`gas_too_low`/
`insufficient_balance`/`invalid_bytecode`/`contract_not_found` 等拒绝码。

---

## 4. 本地启动

需要 Python ≥ 3.10（开发用 3.12）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock       # 可复现：装锁定的确切版本
# 或直接安装本项目（会按 pyproject 拉取兼容版本）：pip install -e .
```

依赖锁定在 `requirements.lock`（`pip freeze` 产出，含全部传递依赖确切版本）。

### 一键演示（自带断言 + 离线回放）

```bash
python -m teachchain.cli demo --db demo.db
```

它会在本地合成账户上依次执行：部署计数器、成功调用、**临界 gas**、
**写后异常**、**写后 REVERT**、**嵌套调用（子失败父成功）**，
逐笔打印状态与费用，最后离线重放整条链并核对状态根。

### 启动 HTTP 服务

```bash
python -m teachchain.cli serve --db teachchain.db --port 8080
# 或：uvicorn teachchain.api:app --port 8080
```

首次启动会给固定合成账户 `alice`/`bob` 注入本地代币（余额仅为演示数字）。

### 离线回放（独立核验）

```bash
python -m teachchain.replay --db teachchain.db --all
```

新建一个干净内核，只重放签名信封，逐字段（status/halt_code/gas/写集/
状态根/版本/结果摘要）与库存收据比对。可在任意机器、任意时间重跑。

### 生成新密钥（唯一使用随机源的命令）

```bash
python -m teachchain.cli keygen --out key.pem   # 权限 600
```

### 手写汇编转字节码

```bash
echo "PUSH 1\nPUSH 2\nADD\nPUSH 0\nMSTORE\nPUSH 1\nPUSH 0\nRETURN" \
  | python -m teachchain.cli asm        # 输出 base64（--hex 输出十六进制）
```

---

## 5. 示例请求（HTTP）

示例已签名交易在 `examples/`（发起方为固定合成账户 alice）。

```bash
# 健康
curl -s http://127.0.0.1:8080/health

# 部署合约
curl -s -X POST http://127.0.0.1:8080/tx \
  -H 'Content-Type: application/json' \
  --data @examples/deploy_tx.json

# 调用合约（把 to 换成上一步返回的 deployed_address，或示例中的地址）
curl -s -X POST http://127.0.0.1:8080/tx \
  -H 'Content-Type: application/json' \
  --data @examples/invoke_tx.json

# 查询
curl -s http://127.0.0.1:8080/receipts/1
curl -s http://127.0.0.1:8080/tx/<tx_hash>
curl -s http://127.0.0.1:8080/accounts/0x37d880d29be2a7ba
curl -s http://127.0.0.1:8080/storage/0xc1fd675f3c628525/1
curl -s -X POST http://127.0.0.1:8080/admin/replay
```

交易信封结构（`sig_b64` 是对 SHA-256(规范化 tx 体) 的 Ed25519 签名）：

```json
{
  "tx": {
    "type": "deploy",
    "from": "0x37d880d29be2a7ba",
    "nonce": 0,
    "gas_limit": 500000,
    "to": null,
    "code_b64": "<base64 字节码>",
    "input": []
  },
  "pub_b64": "<base64 32B Raw Ed25519 公钥>",
  "sig_b64": "<base64 64B 签名>"
}
```

`invoke` 时 `to` 为已部署地址、`code_b64` 为 null、`input` 为 u64 字数组。

拒绝响应统一为 `422 {"error","code","request_id"}`；回放冲突为 `409`。
每个响应都回 `X-Request-ID`（可用同名请求头自行指定）。

---

## 6. 测试

```bash
python -m pytest                 # 全部
python -m pytest -k gas          # 只跑 gas
python -m pytest tests/test_replay_cross_process.py -q
```

测试组织（与生产代码分目录，配置走 `pyproject.toml`/环境变量）：

- `test_gas.py` —— 逐条费用、内存扩张公式、SSTORE 三态与退款、临界 gas；
- `test_integer_bounds.py` —— u64 回绕、除零、比较、栈溢出/下溢、非法跳转、
  返回越界等，断言**具体结果值与具体失败类别**；
- `test_kernel.py` —— 准入拒绝无副作用、nonce、余额、成功/REVERT/OOG 的
  状态与费用各自回滚范围、退款 1/2 截断、状态根、版本/摘要绑定；
- `test_nested_calls.py` —— 子帧 Halt/Revert/成功的写集与 gas 边界、
  深度上限、63/64 转发、调用无代码账户；
- `test_crypto.py` —— 签名/验签、篡改检测、地址派生、规范 JSON；
- `test_replay_cross_process.py` —— **两个独立 OS 进程**重放同一收据链，
  逐字段一致，并硬编码最终状态根；另含“篡改已存收据必被发现”；
- `test_determinism_contracts.py` —— AST 静态扫描 + 50 次重复执行比对；
- `test_api.py` —— FastAPI TestClient 端到端 HTTP；
- `test_diagnostics.py` —— 脱敏与请求标识。

**参考答案独立性**：期望值是手工推导后硬编码的常量（费用逐项相加、状态根
固定哈希、失败类别逐笔锚定）。跨进程测试的参考链由独立脚本
`tests/helpers/build_chain.py` 在另一个解释器进程构建，核验进程不共享内存；
回放内核“只信任签名信封”，不信任被测进程写下的余额/存储索引。

---

## 7. 关键取舍（支持范围与边界）

- **单节点、无共识、无 P2P**：交易高度即“已接受交易序号”，每笔一高度。
- **部署不跑初始化函数**：`deploy` 只做静态解码校验并登记代码，收内在费用，
  未用 gas 全退；避免再引入一套构造器语义。
- **Gas 与本地合成代币 1:1**：无价格市场；余额只来自有序 `credits` 注资流水，
  回放时同样先重放注资再重放交易。
- **SSTORE 用简化工作集计费**（非完整 EIP-2200），但保留 set/reset/noop
  三态与清零退款及 1/2 上限，足以讲清“费用与回滚”。
- **CALL 不传 calldata、不做转账、返回值仅取首字**；地址是 8 字节短地址，
  便于教学阅读。无代码账户调用成功且无副作用（只收 CALL 固定费）。
- **内存按 u64 字寻址**（不是字节），返回数据最多 64 个字，刻意收窄。
- **显式零写落键**，便于审计；状态根对余额/nonce/代码哈希/存储排序后求哈希。
- 失败即终态的两类（Halt/Revert）已区分；不会出现“无法判定”的静默结果——
  无法通过准入的交易直接带错误码拒绝并在诊断日志说明原因。

诊断日志为 JSON 行，带 `level/code/request_id` 及 gas、nonce、height 等
关键字段；`*_b64`、私钥等字段只记录长度（脱敏），地址等假名标识原样保留。

---

## 8. 目录里的运行产物

- `requirements.lock` —— 锁定的完整依赖版本；
- `demo.db*`、`teachchain.db*` —— 本地 SQLite/WAL（运行后生成，已在 .gitignore）；
- 测试报告可在 CI 用 `python -m pytest -v` 直接留存。
