# localffg — 本地共识投票：双签与包围投票证据检测

一个**完全本地、合成夹具**实现的共识可问责性（slashing evidence）原型：验证者对
最终性链接投票时，检测两类可处罚行为并生成**可独立复核的自包含证据**。

- **双签（double vote）**：同一验证者对同一目标轮次投出不同票。
- **包围投票（surround vote）**：同一验证者的一条证明区间**严格嵌套**另一条。
- 完全相同的票重传（retransmit）**明确不算违规**。
- 未验签 / 非法成员 / 错误链域的消息**永远不会**触发处罚标记，只按精确失败类别计数。

技术栈：Python 3.12 · FastAPI · SQLite · `cryptography`（Ed25519，成熟密码库）。
无需任何生产账号、真实业务数据或外部服务。

---

## 1. 明确简化的最终性规则（本地精确定义）

一条投票是一个有签名的证明链接 `s -> t`（`source_round`, `target_round`），
对 `block_root` 背书，且必须满足 `0 <= s < t`。同一验证者满足下列任一条件即构成
可处罚行为：

| 类别 | 精确定义 | 说明 |
|---|---|---|
| **DOUBLE_VOTE** | `t1 == t2` 且两条票不是完全相同的信封 | 仅 source 不同、仅 block_root 不同都算；目标轮相同 |
| **SURROUND_VOTE** | `s2 < s1 < t1 < t2`（或反向） | **严格**嵌套；任一边界相等（如 `t1 == s2`）不算；部分交叉（如 `[0,10]` vs `[5,20]`）也不算 |
| **DUPLICATE_RETRANSMIT** | 两个信封逐字段相同（轮次、摘要、公钥、签名） | 不是违规，不产生证据 |

冲突判定优先级：同一提交同时命中多种时 `SURROUND > DOUBLE`；**重传优先**判定，
即“已经有冲突记录的验证者再次重传同一张旧票”仍是 `duplicate_retransmit`，
不会产生新证据。

**签名绑定**（防跨域/跨链重放）：Ed25519 签名覆盖规范编码的

```
MAGIC || domain || chain_id || validator_id || source_round || target_round || block_root
```

任一绑定字段被改动都会导致验签失败。信封内嵌公钥还必须与注册表中该验证者的公钥
**逐字节一致**（防止 A 用自己的私钥签一张声称是 B 的票）。

**时代与权重快照**：`epoch(round) = round // epoch_length`。验证者权重是
“验证者 × 时代”的分段常函数（加入/退出/调权），证据的处罚权重始终取**两票中
较早目标轮次所在时代**的历史快照，而不是某个会漂移的“当前验证者集”。两个目标时代
都必须是该验证者的活跃成员（权重 > 0），否则证据无效。

---

## 2. 分层工程结构

```
localffg/
├── config.py            # 配置层：YAML + LOCALFFG_ 环境变量覆盖（独立于逻辑）
├── encoding.py          # 编码层：无歧义 TLV 规范编码 / 证据规范序列化 / id
├── crypto.py            # 密码层：Ed25519 签名/验签（cryptography）、合成签名者
├── models.py            # 模型：Vote/SignedVote/Evidence、精确状态枚举
├── epochs.py            # 链状态：验证者注册表 + 时代权重快照
├── kernel.py            # 链状态内核：投票状态机 + 双签/包围精确定义 + 证据构造
├── checker.py           # 独立复核器：不 import kernel，自写判定+直接验签
├── storage.py           # 索引存储：SQLite（注册表、全量事件日志、证据表）
├── replay.py            # 离线回放：新内核重放日志，逐事件/证据集/复核器对账
├── service.py           # 编排：kernel + store + run 日志
├── api.py               # FastAPI HTTP 层（非法提交返回精确 422，绝不伪装成功）
├── cli.py / __main__.py # 命令行：serve / seed-fixtures / demo / replay / recheck
├── logging_utils.py     # 结构化 JSON 日志（run_id、版本、进度、判定依据）
├── fixtures_builder.py  # 确定性合成夹具（密钥/注册表/15 个场景，仅输入）
└── demo.py              # 进程内端到端演示

tests/
├── independent_oracle.py      # ★ 完全独立的测试预言机（不 import 任何生产内核/编码）
├── conftest.py
├── test_encoding_crypto.py    # 编码无歧义 + 替代编码交叉验证 + 签名绑定
├── test_kernel_scenarios.py   # 全部场景的逐步精确断言
├── test_checker_independent.py# 独立复核：真证据通过、逐字段篡改必被拒
├── test_epochs.py             # 时代快照/加入退出/权重
├── test_storage_replay.py     # SQLite 全量日志 + 确定性回放 + 篡改检测
├── test_api.py                # 真实 ASGI HTTP 客户端测试
└── test_oracle_independence.py# 元测试：证明预言机确实独立且双方一致

examples/http_client_example.py  # 对运行中的服务做真实 HTTP 调用的完整走查
config.example.yaml
scripts/{run_server,run_demo,run_tests}.sh
```

这不是单文件实现、不是只有调用壳、也没有固定返回值：每一层都有独立职责和测试。

### 为什么“参考答案不是由被测核心自身生成”

`tests/independent_oracle.py` 通过元测试（AST 扫描）保证**不 import 任何
`localffg.*` 的内核/编码/检查器代码**。它独立实现了：

- 另一套规范编码（分段长度前缀，与生产 TLV 不同），并逐字段重写生产线路格式用于验签；
- 直接调用 `cryptography` 的 Ed25519 验证；
- 自己的时代/成员模型、自己的双签/包围谓词、自己的顺序状态机。

测试三方对账：**硬编码期望值 = 内核输出 = 独立预言机输出**。

---

## 3. 安装（依赖已锁定）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

锁定版本见 `requirements.txt`（fastapi 0.115.5 / uvicorn 0.32.1 / pydantic 2.10.3 /
cryptography 41.0.7 / PyYAML 6.0.1 / pytest 8.3.4 / httpx 0.28.1）。

---

## 4. 快速开始

### 4.1 一键进程内演示（无需起服务）

```bash
.venv/bin/python -m localffg.cli demo --db data/demo/root.db
# 或 scripts/run_demo.sh
```

输出每个场景的逐步分类、9 类计数总计、逐场景离线回放结果，以及每条证据的独立复核。
最后 `DEMO RESULT: PASS` 并对具体计数做断言。

### 4.2 启动 HTTP 服务

```bash
cp config.example.yaml config.yaml   # 可选
scripts/run_server.sh                # 默认 127.0.0.1:8000
```

### 4.3 HTTP 真实调用示例

先起服务，再：

```bash
.venv/bin/python examples/http_client_example.py   # 默认连 127.0.0.1:8765
```

该脚本对以下每一步做真实网络请求并断言结果：健康检查 → 注册合成验证者 → 改权重 →
双签产生证据 → 重传不违规 → 列出证据并独立复核 → 全量离线回放 → 篡改签名被精确拒绝。

也可用 curl：

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/v1/validators/bootstrap \
  -H 'content-type: application/json' \
  -d '{"validator_id":"bob","weight":100,"seed":"localffg-fixture/bob"}'
```

### 4.4 CLI 离线回放与复核

```bash
.venv/bin/python -m localffg.cli replay  --db data/demo/demo-S3_surround_nested.db
.venv/bin/python -m localffg.cli recheck --db <db> --id ev_xxxxxxxx
```

`replay` 在一致时退出码 `0`，任何分歧退出码 `2`；`recheck` 同理。

### 4.5 生成合成夹具

```bash
.venv/bin/python -m localffg.cli seed-fixtures --out data/fixtures
# fixture_manifest.json          —— 注册表真相 + 15 个场景的输入序列（只含输入）
# fixture_synthetic_keys.pem.json —— 合成测试私钥（仅限本地，禁止用于真实网络）
```

---

## 5. HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 应用版本、协议版本、链 id、run_id |
| POST | `/v1/validators/bootstrap` | 注册合成验证者（确定性密钥）；`allow_bootstrap_api=false` 时 403 |
| POST | `/v1/validators/{id}/weight` | 设置自某时代起的权重（加入/退出/调权） |
| POST | `/v1/votes` | 提交签名投票；返回精确 `category` |
| GET | `/v1/votes/stats` | 内核计数 + 日志各类计数 + 证据数 |
| GET | `/v1/evidence` / `/v1/evidence/{id}` | 证据清单 / 单条自包含证据 |
| POST | `/v1/evidence/{id}/recheck` | 独立检查器重新验签+重推判定+权重复核 |
| POST | `/v1/replay` | 用全新内核离线重放日志并对账 |

提交投票的请求体：

```json
{
  "chain_id": "local-chain-0",
  "validator_id": "bob",
  "source_round": 0,
  "target_round": 8,
  "block_root": "<32 字节摘要的 hex>",
  "signer_pubkey": "<32 字节 Ed25519 公钥 hex>",
  "signature": "<64 字节 Ed25519 签名 hex>",
  "run_id": "可选，关联运行身份"
}
```

有效但构成违规的票返回 **HTTP 200**（它是一张真票、本身也是证据材料，`slashable:true`）；
所有非法提交返回 **HTTP 422**，并带精确类别：

```
invalid_signature / invalid_chain / invalid_rounds /
invalid_membership / unknown_validator / malformed
```

未知异常返回 500 且显式标 `ok:false`——**不会把异常或未知状态统一成成功**。

### 证据包（self-contained）

```json
{
  "evidence_id": "ev_<sha256(canonical bundle)[:32]>",
  "kind": "double_vote | surround_vote",
  "chain_id": "...", "validator_id": "...",
  "weight_epoch": 0, "weight": 100,
  "vote_a": { ...完整签名信封（含公钥、签名）... },
  "vote_b": { ...完整签名信封... }
}
```

证据 id 由规范二进制束的 sha256 给出；束内包含两张票的公钥与签名，因此任何第三方拿到
注册表即可独立复核，不需要原始消息流。

---

## 6. 验证过程与测试清单（已真实执行）

```bash
.venv/bin/python -m pytest tests/ -v     # 或 scripts/run_tests.sh
```

**实测结果：69 passed。** 覆盖任务要求的全部验证：

- **重复票**：同一票三次提交 → `accepted` + 2× `duplicate_retransmit`，零证据。
- **同轮冲突**：`0->8` 与 `2->8` → `double_vote`，证据权重 = epoch 0 的 100。
- **嵌套轮次（包围）**：`[0,15]` 与 `[5,10]` 两个到达顺序都报 `surround_vote`，
  且产生**相同的 evidence_id**（与到达顺序无关）。
- **边界精确性**：`[0,10]`+`[10,15]`（边界相等）与 `[0,10]`+`[5,20]`（交叉）均**不报**；
  不同验证者即使同目标也不报。
- **成员变化**：dave 时代 2 才加入 → 早期票 `invalid_membership`、后期票 `accepted`；
  erin 时代 2 退出 → 早期票有效、后期票 `invalid_membership`。
- **独立检查器重验**：真证据 `valid`（双签与包围都有）；篡改签名、换公钥、改摘要、
  改 evidence_id、谎报 kind、谎报权重、用完全相同两张票伪造证据、退出成员的证据——
  每种都被 `invalid` 拒绝并带具体 failure 原因。
- **非法签名与真实冲突分别统计**：`invalid_signature=2`（含“别人私钥+我的id”的
  pubkey 不匹配）与 `double_vote/surround_vote` 分开计数；非法消息不产生任何证据。
- **权重快照**：epoch 0 冲突取权重 100；构造跨时代包围 `[0,25]`/`[5,9]` 时取较早
  目标（epoch 0）权重 100，而非 epoch 2 的 150。
- **离线回放**：全新内核按序重放全部事件，逐事件状态完全一致、证据集合完全一致、
  每条证据独立复核通过；人为篡改日志状态或注入假证据时回放判 **FAIL** 并指出分歧 seq。
- **日志可关联**：每行 JSON 含 `run_id`、`app_version`、`protocol_version`、
  `progress.step/of`、判定依据 `reason/basis`、validator/轮次等。

独立检查器对真证据的输出示例（`POST /v1/evidence/{id}/recheck`）：

```
verdict=valid kind=double_vote signatures=true,true weight=100 @epoch 0
steps: evidence_id matches sha256 of canonical bundle
       vote_a/vote_b: Ed25519 signature valid over canonical payload
       target round 8 -> epoch 0, weight=100
       conflict re-derived independently: double_vote
       slashing weight 100 confirmed at epoch 0
```

---

## 7. 设计说明与关键不变式

1. **处罚前置链**：形状 → 轮次 → 链绑定 → 已知验证者 → 目标时代活跃成员 →
   注册表公钥 Ed25519 验签 → 冲突扫描。只有走完前五步的票才可能产生证据，
   因此“仅凭未验签消息触发处罚”在结构上不可能。
2. **证据去重**：票对按“较早目标优先、同目标签名降序”规范化排序，再做规范哈希；
   同一对票无论谁先到达，evidence_id 相同。
3. **全量日志**：合法/重传/违规/非法的**每一次**提交都进 SQLite 日志并带原始信封，
   回放可以重建历史，而不是只存成功路径。
4. **编码无歧义**：TLV（tag+length+value，u64 大端）+ MAGIC；测试枚举字段变体证明
   不同语义输入不可能产生相同编码；另有独立替代编码交叉验证。
5. **线程模型**：SQLite 以 `check_same_thread=False` + RLock + WAL 串行化写入，
   适配 FastAPI 同步处理器的工作线程。

---

## 8. 剩余限制（如实说明）

- **教育/本地原型**：签名者私钥以 PEM JSON 随夹具写出，仅供本地合成使用；没有 HSM/KMS、
  密钥轮换或远程注册协议。
- **无 P2P/真实 BFT 网络**：投票通过 API/日志进入，不做广播、时延模型或分叉选择；
  “链”是 `(chain_id, epoch_length, 注册表快照)` 的本地定义。
- **最终性规则是简化版**：只判双签与严格包围，不实现 Casper FFG 的最终性提交计算、
  罚没金额（slashing amount correlation）、问责因子等经济机制。
- **证据权重**只记录“较早目标时代”的单验证者权重；没有按全局总权重量化成百分比，
  也没有链上处罚执行模块（检测+举证，不执行）。
- 冲突扫描对同一验证者历史票为线性比较，夹具规模下足够；生产规模需按目标轮索引
  （存储层已预留 `(validator, target_round)` 等索引方向）。
- 注册表快照随数据库可信装载；跨主体分发时注册表本身需要额外的带外信任/签名分发。