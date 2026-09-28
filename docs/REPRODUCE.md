# 复现文档

从零开始复现“本地测试交易的受限栈脚本验证”全部结果。仅需 Python 3.12、
网络可访问 PyPI（首次安装依赖）；所有数据为本地合成夹具。

## 1. 创建环境并安装锁定依赖

```bash
cd opp266/b
python3 -m venv .venv
. .venv/bin/activate
# 精确复现（带制品 sha256 校验）：
pip install --require-hashes -r requirements.lock
# 或宽松复现：
# pip install -r requirements.txt
```

## 2. 生成确定性夹具

```bash
python -m tools.make_fixtures
# → fixtures/keys.json  fixtures/genesis.json  fixtures/cases.json
# 固定种子，重复生成结果逐字节相同（genesis txid 也稳定）。
```

## 3. 运行全部测试（正常 + 异常）

```bash
pytest -q
# 期望：149 passed
```

测试覆盖（断言具体结果与失败类别，而非“接口可调用”）：

- 哈希标准向量 + 纯 Python RIPEMD-160 回退与 OpenSSL 逐字节交叉验证；
- ecdsa（纯 Python）签名、cryptography/OpenSSL 验签的双向交叉验证；
- 重复签名 `SIG_DUPLICATED`、签名顺序颠倒 `THRESHOLD_NOT_MET`、
  错误交易域 `SIG_INVALID`、栈下溢 `STACK_UNDERFLOW`、预算耗尽
  `BUDGET_EXHAUSTED`（验签永不发生）；
- M-of-N 阈值边界（1-of-2、2-of-3、少一个签名、重复、乱序）；
- 四种哈希操作原像（RIPEMD160/SHA256/HASH160/HASH256）；
- 失败用例提交后 UTXO 集、height、日志完全不变；
- 日志篡改 → JOURNAL_CORRUPT；索引与日志分叉 → STATE_ROOT_MISMATCH。

## 4. 查看可重放运行日志

```bash
cat runlogs/test-runs/LATEST           # 最近一次 pytest 的运行编号
ls runlogs/test-runs/<run_id>/
#   summary.json    会话判定
#   events.jsonl    149 条 test_result + 用例日志指针
#   <run_id>-case08/ 每个夹具用例独立目录：
#       trace_step（PC/操作码/预算剩余/栈/备用栈/active）
#       crypto_check（公钥/签名/摘要/布尔结果）
#       verdict + 判定理由
```

用 `grep` 即可复盘任意失败，例如预算耗尽用例最后两条 trace：

```bash
grep trace_step runlogs/test-runs/<run_id>-case08/events.jsonl | tail -2
# 可见 budget_left 从 1 → 0，且不存在 OP_CHECKSIG 事件
```

## 5. 启动服务并真实调用（正常与异常）

```bash
# 终端 A
uvicorn service:app --host 127.0.0.1 --port 8332

# 终端 B
bash examples/curl_examples.sh        # curl 正常/异常调用序列
python examples/client_example.py     # urllib 客户端（无第三方依赖）
```

或不启动服务，直接用 TestClient 跑 HTTP 层测试：

```bash
pytest tests/test_service_http.py -q
```

预期 HTTP 行为：

| 场景 | 状态码 | code |
|---|---|---|
| verify 合法交易 | 200 | OK（状态不变） |
| submit 合法交易 | 201 | OK（UTXO 原子移动） |
| 重复提交同一交易 | 422 | TX_ALREADY_ACCEPTED / STATE |
| 双花已花费 UTXO | 422 | UTXO_MISSING / STATE |
| 重复签名 | 422 | SIG_DUPLICATED / COMPUTE |
| 预算耗尽 | 422 | BUDGET_EXHAUSTED / RESOURCE |
| 未知操作码/元素过大 | 400/422 | UNKNOWN_OPCODE / ELEMENT_TOO_LARGE / INPUT |
| 请求体畸形 | 400 | REQUEST_MALFORMED / INPUT |

服务运行日志写在 `runlogs/service*/<run_id>/`，响应体内带 `run_id`。

## 6. 离线回放

```bash
# 服务/测试提交过若干交易后：
python -m tools.replay --db data/stackvm.db --json
python -m tools.replay --db data/stackvm.db --rebuild-to /tmp/rebuilt.db
```

回放从链式写前日志（每行含 `prev_hash`）重建 UTXO 集并比对状态根：

- 哈希链断裂/负载被改 → `JOURNAL_CORRUPT`；
- 日志完整但重建根 ≠ 存储根 → `STATE_ROOT_MISMATCH`。

## 7. 一次手动端到端（不依赖 pytest）

```bash
python - <<'PY'
import json
from chain.store import Store
from chain import ChainKernel
from stackvm.config import load_settings
from stackvm.transaction import transaction_from_dict

s = load_settings()
store = Store("/tmp/demo.db")
genesis = json.load(open("fixtures/genesis.json"))
store.bootstrap_genesis(genesis, s.chain.mint_total_cap)
k = ChainKernel(store, s)

ok = json.load(open("fixtures/cases.json"))["cases"]["05"]   # 重复签名
r = k.submit(transaction_from_dict(ok["tx"]))
print(r.accepted, r.code.value, r.kind.value, "| height =", store.chain_height)
# → False SIG_DUPLICATED COMPUTE | height = 1   （没有执行转账）
PY
```

## 8. 配置覆盖

编辑 `config/default.toml`，或用环境变量覆盖（无需改文件）：

```bash
STACKVM_LIMITS_OP_BUDGET=50 pytest -q
STACKVM_SIGHASH_DOMAIN_TAG="OTHER.DOMAIN/1" \
  python -m tools.replay --db data/stackvm.db
```
