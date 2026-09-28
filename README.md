# UTXO 测试资产账本

用于测试的最小 UTXO 账本：本地合成身份与数据，Python + FastAPI + SQLite +
成熟密码库（`cryptography`，secp256k1/ECDSA）。支持输入引用、输出、交易费、
多交易区块、块内前序引用、双花/前向/环检测、整数金额守恒与溢出保护；
整块校验失败即整块不提交。

边界语义、错误分类与"不可端到端执行的检查"见 **[docs/semantics.md](docs/semantics.md)**。

## 快速开始

```bash
./verify.sh          # 建虚拟环境 + 固定依赖 + 生成夹具 + pytest + 离线回放
```

或手动：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python scripts/gen_fixtures.py     # 生成 fixtures/
PYTHONPATH=src .venv/bin/python -m pytest tests/            # 全量测试
PYTHONPATH=src .venv/bin/python -m utxo_ledger.replay \
    fixtures/validation_cases.json                          # 离线回放（对照独立 oracle）
```

启动 HTTP 服务（可选）：

```bash
PYTHONPATH=src .venv/bin/uvicorn utxo_ledger.api:create_app --factory --reload
```

## 目录结构

```
src/utxo_ledger/
  errors.py      四类错误契约（INPUT_ERROR / STATE_CONFLICT /
                 RESOURCE_EXHAUSTED / COMPUTATION_FAILED）
  encoding.py    固定二进制编码、严格 JSON、txid、签名摘要、哈希链根
  crypto.py      secp256k1 ECDSA 验签、确定性测试密钥
  store.py       SQLite 索引 + 花费审计 + 单事务原子提交
  kernel.py      链状态内核（逐笔规划，不通过不提交）
  journal.py     run_id / JSONL 运行日志 / 状态保全结论
  replay.py      离线回放 CLI（夹具 ↔ 独立 oracle 对照）
  fab.py         本地合成夹具构造器
  api.py         FastAPI 边界
reference/
  oracle.py      独立参考实现（不 import 被测包；stdlib + cryptography）
scripts/
  gen_fixtures.py 用 oracle 计算预期、生成夹具
fixtures/
  validation_cases.json  2 前置块 + 14 用例（2 接受 / 12 拒绝）
  keys.json              5 个确定性合成公钥
tests/           8 个测试文件（编码/验签/内核正负/存储/差分/回放/API）
docs/semantics.md 边界语义与不可执行检查清单
logs/            每次运行的 <run_id>.jsonl（验证后生成）
```

## 测试覆盖的关键攻击（断言具体类别，而非"接口可调"）

| 用例 | 类别 | code |
| --- | --- | --- |
| 块内两笔交易双花同一 UTXO | STATE_CONFLICT | DOUBLE_SPEND (tx=1) |
| 单笔交易重复输入 | STATE_CONFLICT | DOUBLE_SPEND (tx=0) |
| 零值输出 | INPUT_ERROR | ZERO_VALUE |
| 签名篡改 | COMPUTATION_FAILED | SIGNATURE_INVALID |
| 历史双花 / 未知 outpoint | STATE_CONFLICT | DOUBLE_SPEND / UNKNOWN_OUTPOINT |
| 前向引用 | STATE_CONFLICT | FORWARD_REFERENCE |
| 价值不守恒 / 费用异常 / 非 genesis 发行 | INPUT_ERROR | CONSERVATION_MISMATCH / INVALID_FEE / ILLEGAL_ISSUE |
| 金额求和溢出 | INPUT_ERROR | AMOUNT_OVERFLOW |
| 见证数不符 / 严格 JSON | INPUT_ERROR | WITNESS_COUNT_MISMATCH / MALFORMED_ENCODING |
| 块高度跳变 | STATE_CONFLICT | BLOCK_CONFLICT |
| tx_root/witness_root 不符 | COMPUTATION_FAILED | ROOT_MISMATCH |
| 块/交易/输入输出条数超限 | RESOURCE_EXHAUSTED | RESOURCE_LIMIT |

每次拒绝都断言提交前后 `(height, tip, utxo_count, utxo_root, block_count)`
完全一致（整块不提交）。

## 参考答案的独立性

`reference/oracle.py` 不 import `utxo_ledger` 包：它用 stdlib 的 `hashlib/struct`
独立重写字节布局，用独立的函数式 dict 状态机逐笔判定，仅复用 `cryptography`
做 secp256k1 验签。夹具 `expected_*` 由 oracle 计算；`tests/test_06_oracle_diff.py`
再用参数化变体做差分，避免"答案全部由被测核心自身生成"。

## HTTP 摘要

`POST /blocks` 提交整块 JSON，成功 201，失败返回带 `run_id`、`error.category/code`、
`state_preserved` 的信封；另有 `GET /chain/tip`、`GET /utxo/{txid}/{vout}`、
`GET /address/{pubkey}/utxos`、`GET /health`。
