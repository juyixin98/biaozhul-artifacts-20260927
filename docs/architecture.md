# 架构：四个真实模块 + 独立配置

```
                 ┌─────────────────────────────────────────────┐
   HTTP/JSON     │ api/app.py        FastAPI 服务（verify/submit │
  ─────────────▶ │                   /utxos/runs/replay）        │
                 └───────────────┬─────────────────────────────┘
                                 │
                 ┌───────────────▼─────────────────────────────┐
                 │ chain/kernel.py  链状态内核（模块二）         │
                 │  结构→状态前置→金额→逐输入脚本→原子上链       │
                 └───────┬───────────────────────┬─────────────┘
                         │                       │
        ┌────────────────▼─────────┐   ┌─────────▼────────────────┐
        │ encoding/（模块一）       │   │ storage/store.py（模块三）│
        │  opcodes / script_codec  │   │  SQLite: utxo/spent/runs │
        │  crypto / transaction    │   │  /meta + 状态根          │
        └────────────────┬─────────┘   └──────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │ vm/stack_machine.py │  受限栈机（元素/步/深度预算）
              └─────────────────────┘

   replay/__init__.py + scripts_cli.py   离线回放（模块四，内存 SQLite）
   config.py                              独立配置（env 可覆盖）
```

## 模块边界

1. **编码与验签 `rsv.encoding`**
   - `opcodes.py`：唯一操作码白名单；
   - `script_codec.py`：严格解析（最小推送、白名单、静态分支配对/深度）；
   - `crypto.py`：域标签 sighash、secp256k1 验签，全部委托成熟库
     `cryptography`（hazmat），含供测试对拍的 `cross_check_signature`；
   - `transaction.py`：交易强类型与确定性规范化序列化（witness 不进摘要）。

2. **链状态内核 `rsv.chain`**
   - 前置状态检查（UTXO 存在/未花/域一致）、金额守恒、逐输入执行栈机；
   - 通过后在单事务内删输入 UTXO、登记 spent、写输出 UTXO；
   - 任何失败均在变更前抛出，run 记录落 SQLite 与 `runs/runs.jsonl`。

3. **索引存储 `rsv.storage`**
   - 纯持久化/索引，不含脚本逻辑；表：`meta / utxo / spent / runs`；
   - `state_root()` 对 UTXO 集合做确定性 SHA256，供回放对账。

4. **离线回放 `rsv.replay` + `scripts_cli.py`**
   - 全新内存 SQLite 上用同一内核顺序重放 bundle；
   - 输出每笔判定、失败分类、最终状态根；CLI 退出码区分全接受(0)/有拒绝(2)。

## 独立配置 `rsv.config`

资源上限、网络/域标签、clean-stack、DB/日志路径集中在 `config.py`，
支持 `RSV_*` 环境变量覆盖（见下表）。生产语义无关——所有默认值都偏小，
便于在测试中触发边界。

| 环境变量 | 默认 |
|---|---|
| `RSV_MAX_ELEMENT_SIZE` | 520 |
| `RSV_MAX_STACK_ITEMS` | 64 |
| `RSV_MAX_OP_STEPS` | 128 |
| `RSV_MAX_SCRIPT_DEPTH` | 8 |
| `RSV_MAX_SCRIPT_BYTES` | 2048 |
| `RSV_MAX_MULTISIG_PUBKEYS` | 16 |
| `RSV_NETWORK` | rsv-local |
| `RSV_DEFAULT_DOMAIN` | rsv-test-domain-v1 |
| `RSV_SQLITE_PATH` | ./data/rsv.sqlite3 |
| `RSV_RUNS_DIR` | ./runs |

## 参考答案独立性

夹具与每个测试向量的预期答案由 `tools/generate_fixtures.py` 生成。该脚本：

- **不 import 任何 `rsv.*` 被测代码**（可 `grep -R "import rsv" tools/` 验证）；
- 自带第二份规范化序列化、sighash、推送解析与模板级状态机；
- 验签真值直接取自成熟库 `cryptography`，哈希真值直接取自 `hashlib`。

测试再拿这些外部真值与栈机/内核结果对拍，避免“参考答案由被测核心自身生成”。
