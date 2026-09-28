# 受限以太坊 ABI 编解码后端

一个从零实现的以太坊 ABI（应用二进制接口）**受限类型**编解码后端，覆盖
整数、定长/动态字节串、定长/动态数组与元组；在此之上构建了确定性的
本地“伪链”账本、SQLite 索引存储、HTTP API 与离线回放。全部数据为
本地合成夹具，不依赖真实账号或业务数据。

- **语言/框架**：Python 3.12 · FastAPI · SQLite · pycryptodome（Keccak）
- **预言机**：测试用成熟库 `eth_abi` 逐字节对照（不参与被测核心实现）
- **精确依赖锁定**：[`requirements.lock`](requirements.lock)

---

## 1. 目录结构（按职责分层）

```
app/
  abi/            编码与验签内核（本项目核心，无框架依赖）
    types.py        受限类型系统与解析器
    encoder.py      严格规范编码（head/tail 两遍）
    decoder.py      严格安全解码（边界检查/重叠/越界/填充校验）
    hashing.py      Keccak-256（pycryptodome，eth_hash 交叉校验）
    call.py         函数选择器与 calldata 编解码
    errors.py       稳定的失败类别体系
  kernel/         链状态内核：确定性伪合约方法、区块、状态根
  storage/        SQLite 索引存储（参数化查询、schema 版本）
  replay/         离线回放：从夹具重建状态并强断言
  api/            FastAPI HTTP 层（结构化错误信封 + 请求日志）
  config.py       配置层（环境变量覆盖）
  runlog.py       结构化运行日志（run_id / 版本 / 步骤 / 判定）
scripts/
  generate_golden_vectors.py  用 eth_abi 生成黄金向量 + 手工恶意向量
  replay.py                     离线回放 CLI
  run_dev.sh / example_requests.sh
tests/            独立测试层（pytest），fixtures/ 为合成夹具
```

这不是单文件实现：ABI 内核、链内核、存储、回放、API 各自独立，可单测。

---

## 2. 本地启动

```bash
# 方式 A：一键脚本（建虚拟环境、装锁定依赖、生成夹具、起服务）
bash scripts/run_dev.sh

# 方式 B：手动
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
python scripts/generate_golden_vectors.py     # 生成黄金向量
ABI_DB_PATH=data/chain.db python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/healthz
# {"ok":true,"version":"1.0.0","run_id":"dev-..."}
```

环境变量（见 `app/config.py`）：`ABI_DB_PATH`、`ABI_HOST`、`ABI_PORT`、
`ABI_LOG_DIR`、`ABI_FIXTURE_PATH`、`ABI_MAX_DECODE_BYTES` 等。

---

## 3. 示例请求

完整脚本见 `scripts/example_requests.sh`。摘选：

```bash
# 知名 ERC-20 选择器（验证 Keccak）
curl -s localhost:8000/abi/selector -H 'content-type: application/json' \
  -d '{"signature":"transfer(address,uint256)"}'
# {"ok":true,"selector":"0xa9059cbb", ...}

# 编码嵌套动态类型 (string[],uint256)
curl -s localhost:8000/abi/encode -H 'content-type: application/json' \
  -d '{"types":["(string[],uint256)"],"values":[[["a","bb"],7]]}'

# 链：铸造
curl -s localhost:8000/chain/transact -H 'content-type: application/json' -d '{
  "signature":"mint(bytes20,uint256)",
  "args":[{"bytes":"0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},1000]}'

# 恶意：bytes 声明 2^256-1 长度 -> 400 + allocation_limit（不发生巨大分配）
curl -s localhost:8000/abi/decode -H 'content-type: application/json' -d '{
  "types":["bytes"],
  "data":"0x000...0020 ffff...ffff"}'
```

HTTP 中二进制参数用 `{"bytes":"0x.."}` 标签传递；大整数在 JSON 响应里
以字符串返回以保证前端精度。

---

## 4. 支持范围与关键取舍

### 支持（受限类型）
| 类型 | 说明 |
|---|---|
| `int<N>` / `uint<N>` | N = 8..256，步长 8；严格符号扩展与值域 |
| `bytes<N>` | N = 1..32，右填充必须为 0 |
| `bytes` / `string` | 动态；string 强制 UTF-8 |
| `T[k]` / `T[]` | 定长/动态数组，可多维、可嵌套动态 |
| `(T1,T2,…)` | 元组，可任意嵌套 |

明确**不支持**（解析期返回 `unsupported_type`）：`address`、`bool`、
`fixed/ufixed`、`function`。链方法中的“地址”用 `bytes20` 表达。
选择器计算仍识别 `address/bool`（它们有标准 canonical 名），因为选择器
只对类型签名取哈希，与参数编解码是两件事。

### 关键布局语义（已用 eth_abi 逐字节验证）
1. **偏移按容器基准解释**：成员偏移始终相对“所在容器自己头部的第一字节”。
   元组即元组头首字节；动态数组是长度字之后的头首字节。嵌套动态因此
   使用容器相对偏移，**绝不**把内层偏移当成整个 blob 的绝对文件偏移。
2. **动态块严格首尾相接**：解码时动态子块按偏移排序，必须紧贴铺满尾部。
   指入静态头/与前块重叠 → `overlap`；留有未声明间隙 → `non_canonical_layout`。
3. **顶层严格消费**：比 eth_abi 更严格——尾随字节一律拒绝
   （eth_abi 默认容忍顶层 trailing bytes）。
4. **整数规范填充**：uint 高位必须全 0；int 必须正确符号扩展；
   `uint8=256`、错误符号位等都归 `non_canonical_padding`。
5. **资源上限**：输入 ≤ 1 MiB、单数组元素 ≤ 32768、字节串长度受限；
   所有外部 length/offset **先比较上限再分配**，恶意 2^256 长度只命中
   `allocation_limit`，测试用 `tracemalloc` 断言不发生巨大分配。

### 失败类别（稳定字符串，测试与日志直接引用）
`invalid_type` · `unsupported_type` · `value_error` · `length_mismatch` ·
`offset_out_of_bounds` · `overlap` · `non_canonical_padding` ·
`non_canonical_layout` · `allocation_limit` · `depth_limit`，
链业务另有 `insufficient_balance` / `arithmetic_overflow` 等。
**异常或未知状态绝不会被统一返回成成功**：API 返回 `{ok:false,error:{category,...}}`。

---

## 5. 测试与诊断

```bash
. .venv/bin/activate
python -m pytest                       # 全部 58 个测试
python -m pytest -m oracle             # 仅 eth_abi 黄金对照
python -m pytest -m security           # 仅恶意输入/资源限制
python -m scripts.replay --fixture tests/fixtures/chain_fixture.json
```

- **黄金向量不来自被测核心**：`scripts/generate_golden_vectors.py` 用
  成熟 `eth_abi` 编码 46 个有效向量（含嵌套动态数组、空串/空 bytes、
  负整数、多字节 UTF-8），测试断言我们的编码**逐字节相等**、解码
  eth_abi 字节得到相同值；另有 18 个**手工构造**的恶意向量断言**具体
  失败类别**。选择器用钉死的知名 ERC-20 值（`a9059cbb` 等）+ 两个
  Keccak 实现交叉验证。
- **断言具体结果与类别**，不是“接口能调用”。恶意偏移断言不越界读取、
  不巨大分配（`tracemalloc` 限定净堆增长）。
- **运行日志**在 `test-logs/<run_id>.jsonl`：每行含 `run_id`、时间戳、
  `stage/event`、输入指纹或账户标识、关键步骤（区块内逐笔 calldata、
  状态根）、`verdict`、失败 `category` 以及 Python/库版本。响应头
  `x-run-id` 可把一次 HTTP 调用关联到日志。

离线回放对余额、回滚类别序列、最终状态根做强断言；篡改期望值会以
非零退出码失败（`balance_mismatch` 等），并在日志记 `failure`。

---

## 6. 链状态内核说明（本地伪 EVM）

四个确定性方法（全部受限类型，地址用 bytes20）：
`mint(bytes20,uint256)`、`transfer(bytes20,bytes20,uint256)`、
`setNote(bytes20,string)`、`noteOf(bytes20)`。失败交易不改状态，
回执 `status="reverted"` 并带类别。状态根用我们自己的 ABI 内核编码
账户表后取 Keccak，完全可复算；区块哈希链式提交父哈希。
