# 可撤回的链重组派生索引（Revocable Reorg-Derived Index）

一个**完全本地、可审查**的合成区块链派生索引实现：接收已密封区块，按父哈希连接成链，
在分叉时依据**固定权重规则**选择最佳链，切换时**先撤旧链、再加新链**，并拒绝越过
**最终性边界**的重组。派生账户索引（余额/nonce/事件）是区块事件的可撤回投影，重组后
与从当前最佳链**全量重建**的结果严格一致。

技术栈：Python 3.12 · FastAPI · SQLite（WAL，外键）· PyCA `cryptography`（Ed25519 + SHA-256）。
无生产账号、无外部业务数据，所有参与者和数据均由本地夹具合成。

## 30 秒复现

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.lock
pip install -e .

# 一键：重新生成夹具 → 离线回放（含深分叉拒绝）→ 重建一致性 → 全部测试
bash scripts/run_all.sh
```

输出应包含 `62 passed`，且重建校验打印 `"consistent": true`。

也可单独运行：

```bash
# 离线回放（不需要 HTTP 服务）
python -m reorgindex.replay.cli replay fixtures/short_fork/recording.json \
    --db run/short_fork.db --report run/reports/short_fork.json
python -m reorgindex.replay.cli verify fixtures/short_fork/recording.json \
    --db run/short_fork.db

# 启动本地服务（真实 uvicorn），另开终端调用
REORG_DB_PATH=run/live.db python -m reorgindex.api.serve
python scripts/call_service_example.py     # 进程内 TestClient 版本
```

curl 示例见 [`docs/REPRODUCE.md`](docs/REPRODUCE.md)。

## 模块职责（不是单文件脚本）

```
src/reorgindex/
├── config.py                 # 环境变量配置：DB 路径、最终性深度 K、允许难度集合
├── app.py                    # 装配：store + diagnostics + engine
├── crypto/                   # 成熟密码库封装
│   ├── encoding.py           # 规范 JSON（签名/哈希的唯一字节编码）
│   ├── keys.py               # Ed25519 密钥、地址、签名/验签
│   └── hashing.py            # txid、Merkle 根、区块恒等哈希、PoW 目标
├── kernel/                   # 链状态内核（无 I/O 依赖的纯规则）
│   ├── models.py             # 交易/区块的构造与签名封装
│   ├── errors.py             # 稳定拒绝类别码（测试断言这些码）
│   ├── validation.py         # 无状态校验：编码、Merkle、PoW、签名、生产者授权
│   ├── state.py              # 有状态账本：nonce 顺序、余额、链内重复 txid
│   ├── derivation.py         # 由区块派生有序账本事件（撤回 = 逆事件）
│   └── engine.py             # 摄入、孤儿挂起/释放、分叉选择、最终性、两阶段切换
├── storage/
│   ├── schema.py             # SQLite 表结构
│   └── store.py              # 派生索引、活动链、孤儿表、持久化 switch_plan、诊断
├── replay/                   # 离线回放（与在线摄入共用同一内核）
│   ├── builder.py            # 本地合成链构建器（挖矿/签名/分叉）
│   ├── fixture_io.py         # 夹具读写
│   ├── rebuild.py            # 从夹具全量重建到新 DB
│   ├── verify.py             # 在线索引 vs 全量重建一致性
│   └── cli.py                # `python -m reorgindex.replay.cli`
├── api/
│   ├── main.py               # FastAPI 路由
│   ├── deps.py               # 依赖装配
│   └── serve.py              # uvicorn 启动入口
└── diag/
    ├── masking.py            # 脱敏：签名/公钥/地址/哈希的打印规则
    └── logger.py             # 结构化诊断（请求标识 + 关键状态 + 接受/拒绝原因）

tests/
├── conftest.py               # 夹具加载、Application 工厂
├── oracle/reference.py       # 独立参照实现（刻意不 import 被测内核）
├── test_crypto.py            # 编码/哈希/签名（含篡改拒绝）
├── test_validation.py        # 每类无效输入的具体拒绝类别
├── test_state.py             # nonce/余额/重复 txid 的状态语义
├── test_storage.py           # 派生索引、原子两阶段、崩溃恢复、读者一致性
├── test_scenarios.py         # 四个复核场景 + 与 oracle 全量对账
├── test_edges.py             # 平局规则、最终性边界、查询只见活动链
├── test_replay.py            # 离线回放与全量重建一致性
├── test_api.py               # HTTP 正常/异常路径
└── test_diagnostics.py       # 脱敏与请求标识
```

## 固定规则（模型，而非可随意调的“实现细节”）

* **区块连接**：区块头含 `parent`；父未知 → 进入 `pending_blocks` 挂起（不拒绝），父到达后
  按到达顺序级联释放。
* **权重/分叉选择**：区块权重 = 区块头声明难度（合成夹具允许集合 `{4,16}`），
  链累计权重为创世到 tip 的权重之和；累计权重更大者胜，**平局时 tip 哈希字典序更小者胜**
  （确定、与到达时间无关）。
* **最终性**：区块确认数 = `tip_height - block_height + 1`；确认数 `> K`（`K=3`）为最终。
  会撤下任何最终区块的切换以 `REORG_FINALIZED` 拒绝，并给出回滚高度区间。
* **切换顺序**：持久化计划（`switch_plan`, phase=DETACHED）→ 逆序撤旧链事件并提交 →
  顺序加新链事件并提交并清除计划。查询永远只见完整的旧版本或完整的新版本。
* **重复交易**：同一 `txid` 在两条分支出现时，`tx_locations` 记录每次出现，
  但同一链视图内有状态重放会以 `DUPLICATE_TXID` 拒绝第二次贡献；切换后旧出现置
  `on_active=0`，**最佳链上恰好一个有效贡献**。

## 诊断

每次接受/挂起/拒绝都落 `diagnostics` 表并打印一行 JSON：含 `request_id`（可用
`X-Request-ID` 头传入）、候选块哈希（截断打印、全量入库）、高度/父哈希、当前 tip 与高度、
累计权重，以及拒绝时的稳定类别码与“为什么”。私钥、签名、公钥永不出现在日志
（见 `diag/masking.py` 的测试）。
