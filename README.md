# RSV — 本地测试交易的受限栈脚本验证器

实现一个**受限**栈脚本验证系统：常量/推送、哈希（SHA256/HASH256/HASH160/
RIPEMD160）、ECDSA-secp256k1 验签与 m-of-n 门槛，交易签名绑定**交易摘要 +
域标签**。仅用于本地合成测试，**不兼容任何完整链**（支持的操作码见
[`docs/opcodes.md`](docs/opcodes.md)）。

## 验收规则对应

1. **唯一操作码白名单**：`src/rsv/encoding/opcodes.py`，表外字节解析期拒绝
   （`input.unknown_opcode` / `input.reserved_opcode`）；文档明确不兼容完整链。
2. **签名绑定摘要+域标签、公钥不重复计数**：见 `docs/opcodes.md` 的 sighash
   与 m-of-n 两节；重复签名/重复策略公钥 → `compute.crypto.threshold_invalid`。
3. **资源受限**：栈元素 ≤520B、栈 ≤64 项、步数 ≤128、分支深度 ≤8、
   脚本 ≤2048B（`src/rsv/config.py`，env 可覆盖）。
4. **失败只返回分类，不转账**：四类 `input/state/resource/compute`
   （`docs/failure-codes.md`）；状态更新只在验证全过后的单个 SQLite 事务内进行，
   测试逐字节断言失败前后状态根不变。

## 模块划分（均为真实模块，无桩）

| 模块 | 路径 |
|---|---|
| 独立配置 | `src/rsv/config.py` |
| ① 编码与验签 | `src/rsv/encoding/`（opcodes, script_codec, crypto, transaction） |
| ② 链状态内核 | `src/rsv/chain/kernel.py` + 栈机 `src/rsv/vm/stack_machine.py` |
| ③ 索引存储 | `src/rsv/storage/store.py`（SQLite：utxo/spent/runs/meta + 状态根） |
| ④ 离线回放 | `src/rsv/replay/__init__.py`、`src/rsv/scripts_cli.py` |
| HTTP 服务 | `src/rsv/api/app.py`（FastAPI） |

## 快速复现

需要 Python 3.11+（开发用 3.12）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt      # 锁定版本，可复现

# 1) 生成独立参考答案夹具（不 import 被测代码，仅 stdlib + cryptography）
.venv/bin/python tools/generate_fixtures.py

# 2) 全量测试（59 个，断言具体结果与失败码，含成熟库交叉验证）
PYTHONPATH=src .venv/bin/python -m pytest tests/ -v

# 3) 直接内核调用示例（内存 SQLite，不起服务）
PYTHONPATH=src .venv/bin/python examples/direct_kernel.py

# 4) 离线回放 bundle（不依赖网络/服务）
PYTHONPATH=src .venv/bin/python -m rsv.scripts_cli \
    fixtures/bundles/failure_catalog.json --runs-dir runs/replay
#   退出码 0=全接受；2=存在被拒交易（预期失败目录返回 2）

# 5) HTTP 服务
RSV_SQLITE_PATH=$PWD/data/rsv.sqlite3 \
PYTHONPATH=src .venv/bin/python -m uvicorn rsv.api.app:app --port 8791
#   另一终端：RSV_BASE=http://127.0.0.1:8791 bash examples/curl_service.sh
```

## HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 网络、域、资源上限、状态根 |
| `POST /bootstrap` | 载入创世纪 UTXO（重复载入同 id 幂等，冲突 → `state.bootstrap_conflict`） |
| `POST /tx/verify` | 只验证，不改状态（dry-run） |
| `POST /tx/submit` | 验证通过才原子上链；失败只回分类 |
| `GET /utxos?domain=` | UTXO 列表与状态根 |
| `GET /runs/{run_id}` | 按运行编号取回类别、理由、逐指令轨迹 |
| `POST /replay` | 内存状态离线重放 bundle |

业务失败统一是 `200 + {"accepted": false, "failure": {category, code, ...}}`，
携带 `run_id`；仅请求体不是合法 JSON 时返回 400。

## 测试设计要点

- **独立参考答案**：`tools/generate_fixtures.py` 不 import 任何 `rsv.*`，自带
  第二份规范化序列化/sighash/推送解析/模板状态机，期望值直接调
  `cryptography`（ECDSA）与 `hashlib`（SHA 系列）得出；测试拿它和栈机/内核对拍。
- **任务点名的异常路径**都有精确断言：
  - 重复签名 → `compute.crypto.threshold_invalid`
  - 2-of-3 签名顺序 c/a vs a/c → 都接受，最终栈与步数完全一致
  - 错误交易域 → 状态层 `state.domain_conflict`；域正确但签名在错误域摘要上
    → `compute.crypto.sig`
  - 栈下溢 → `compute.stack_underflow`
  - 预算耗尽（129 NOP vs 128）→ `resource.op_budget_exhausted`
  - 阈值边界：1-of-3 接受、有效数不足 → `compute.crypto.threshold`、
    m=0 / m>n / 重复公钥 → `threshold_invalid`
  - 哈希：栈机结果与 `hashlib` 三方对拍（夹具期望值、hashlib、栈机）
- **失败不转账**：`test_rejected_tx_changes_nothing` 对失败目录中每条交易断言
  UTXO 集合与状态根逐字节不变；双花 bundle 断言第二笔失败后根不变。

## 可复核运行产物

- `artifacts/pytest_results.txt` — 全量测试详细输出（59 passed）
- `artifacts/example_direct_kernel.txt` — 直接内核调用真实输出
- `artifacts/example_service.txt` — 真实 HTTP 服务调用输出
- `artifacts/server.log` — 服务启动日志
- `runs/runs.jsonl` — 每次验证的运行编号、类别、判定理由、message32、
  逐指令中间轨迹（可脱离 DB 重放问题）；结构化记录同时在 SQLite `runs` 表
