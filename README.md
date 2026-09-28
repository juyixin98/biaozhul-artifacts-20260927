# 固定本地测试链 · 头部轻客户端

> **范围声明**：这是一个**简化的、自包含的本地测试链协议**
> （`local-header-lightclient/v1`），用于教学和验证“受信检查点 + 委员会
> 阈值签名”头部同步的安全边界。它**不是任何现有公链的兼容客户端**，
> 不连接任何真实网络，所有密钥、委员会、区块均来自**本地确定性合成夹具**。

技术栈：Python 3.12 · FastAPI · SQLite · [`cryptography`](https://cryptography.io/)（Ed25519）

---

## 1. 它验证什么（安全规则一览）

内核（`lightclient/kernel.py`）的每次头部接受必须**全部**满足；任何一条失败都在**写入前拒绝**：

1. **受信根唯一来源**：只能由固定的带外受信检查点公钥验签的检查点引导（`bootstrap`）。已引导后再次提交检查点一律 `ALREADY_INITIALIZED` 拒绝——**头部永远不能直接更新根**。
2. **父链接**：新头的 `parent_digest` 必须等于**当前可信 tip**。
   - 父哈希已知但不是 tip（侧链）→ `CONFLICTING_HEADER`（拒绝切换到未信任分支）；
   - 父哈希未知 → `PARENT_UNKNOWN`；
   - 高度跳跃（缺中间头）→ `UNTRUSTED_BRANCH`。
3. **轮次单调**：`round` 必须沿可信链**严格递增**，否则 `ROUND_NOT_MONOTONIC`。
4. **时间戳单调 + 信任期**：时间戳严格递增，且
   `header.timestamp - tip.timestamp <= trust_period_seconds`。
   - 恰在边界（`==`）**接受**；
   - 超过哪怕 1 秒 → `NEED_CHECKPOINT`（长离线后必须要新检查点，而不是“悄悄接上”）。
5. **权重门槛**：证书中每个委员会成员最多计一次，按成员**权重**求和，必须 `>= quorum_weight`。权重不足 → `WEIGHT_BELOW_QUORUM`（返回实际签名权重/门槛/人数）。
6. **委员会变更由前一有效委员会授权**：
   - 新委员会必须在 epoch `e` 的最后一个头里**预告**，且该预告头本身持有当前 epoch `e` 委员会的阈值证书；
   - epoch `e+1` 的第一个头必须由被预告的委员会签名；
   - 上一代委员会给新时代头签名 → `STALE_COMMITTEE`；
   - 未预告即换纪元 → `COMMITTEE_UNKNOWN`；跳纪元 → `COMMITTEE_UNKNOWN`；
   - 预告纪元不是 `e+1`、门槛与策略不符、总权重永远到不了门槛 → `COMMITTEE_BAD_TRANSITION`。
7. **链绑定**：`chain_id` 不匹配 → `CHAIN_MISMATCH`。

所有接受在**单条 SQLite 事务**内落盘（头 + 证书 + 新委员会 + tip 元数据），
因此拒绝或崩溃都不会留下半截 tip。

## 2. 错误语义（四类可区分失败）

所有错误都通过同一信封返回（HTTP/内核/审计一致）：

```json
{"ok": false, "error": {"code": "...", "category": "...", "reason": "...", "detail": {...}}}
```

| category  | 含义 | HTTP | 代表 code |
|-----------|------|------|-----------|
| `input`   | 输入数据本身 malformed / 验签失败，与状态无关 | 400 | `INPUT_MALFORMED`、`SIGNATURE_INVALID`、`CHECKPOINT_SIGNATURE_INVALID`、`CHAIN_MISMATCH` |
| `state`   | 数据合法但违反协议授权或与可信状态冲突；**不改变状态** | 409 | `PARENT_UNKNOWN`、`UNTRUSTED_BRANCH`、`CONFLICTING_HEADER`、`ROUND_NOT_MONOTONIC`、`TIMESTAMP_NOT_MONOTONIC`、`WEIGHT_BELOW_QUORUM`、`STALE_COMMITTEE`、`COMMITTEE_UNKNOWN`、`COMMITTEE_BAD_TRANSITION`、`NEED_CHECKPOINT`、`NOT_INITIALIZED`、`ALREADY_INITIALIZED` |
| `resource`| 命中配置的资源上限；**不改变状态** | 413 | `RESOURCE_LIMIT`（请求体/头部/票数/委员会人数/回放批量） |
| `compute` | 密码/运行时计算失败（安全致命）；**不改变状态** | 500 | `COMPUTE_FAILED` |

code→category 的唯一映射表见 `lightclient/errors.py`。

## 3. 工程边界（模块与数据/错误契约）

| 模块 | 职责 | 不做什么 |
|------|------|----------|
| `lightclient/types.py` | 值类型（`Header`/`Committee`/`Certificate`/`Checkpoint`），表示级不变量 | 不做协议判断 |
| `lightclient/codec.py` | 确定性 v1 二进制编解码、标签域分离、严格解码、摘要/签名消息定义 | 不验签、不查状态 |
| `lightclient/crypto.py` | Ed25519 签名与**加权阈值**证书验证 | 不查链状态 |
| `lightclient/store.py` | SQLite 索引存储（头/委员会/证书/元数据/审计），单事务原子落盘 | 不含协议规则 |
| `lightclient/kernel.py` | **链状态内核**：引导、全部安全规则、拒绝前不写入、审计 | 不处理 HTTP |
| `lightclient/replay.py` | 离线顺序回放：preflight 资源检查、遇拒即停、结构化报告 | 不绕过内核 |
| `lightclient/service.py` | FastAPI 边界：JSON↔类型、错误→HTTP 状态映射 | 不含安全逻辑 |
| `lightclient/fixtures/` | **独立**合成夹具/参考构造器（仅依赖 codec+crypto+types） | 不导入 kernel/store |

**“参考答案不由被测核心自己生成”**：测试期望来自
`lightclient/fixtures/builder.py`（独立构造器）和
`tests/generate_golden.py`（用 `hashlib.sha256` 直接对规范字节重算摘要、
直接调用 `cryptography` 验签、纯整数权重求和），该脚本与内核完全隔离。

## 4. 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 端到端内核演示（连续合法头 / 权重不足 / 旧委员会 / 冲突头 / 未知父 / 信任期边界）
.venv/bin/python demo.py

# HTTP 服务演示（进程内 TestClient，不走外网）
.venv/bin/python demo_http.py

# 离线回放演示，并写出可复现制品到 replay-artifacts/<run_id>/
.venv/bin/python demo_replay.py
```

启动真实监听服务（默认 `127.0.0.1:8088`）：

```bash
export LC_CHECKPOINT_KEY_HEX=8db19dd273f3be55bf6f829e38a9aba2388c9c05b58170441d6dfcfa20a72639
export LC_DB_PATH=./data/lc.db
.venv/bin/python -m lightclient.run
```

> 上面的检查点公钥是确定性合成夹具 `ChainBuilder()` 的固定受信公钥
> （`SHA256("lc-fixture-key|trusted-checkpoint")` 派生），仅用于本地演示。

主要环境变量：`LC_CHAIN_ID`、`LC_TRUST_PERIOD`（秒，默认 3600）、
`LC_QUORUM_WEIGHT`（默认 2）、`LC_DB_PATH`、`LC_CHECKPOINT_KEY_HEX`。

### HTTP 接口

| 方法/路径 | 说明 |
|-----------|------|
| `GET  /health` | 初始化状态、协议标记、tip |
| `POST /bootstrap` | body `{"checkpoint_envelope": <hex>}`，仅一次 |
| `POST /headers` | body `{"header": <hex>, "certificate": <hex>, "run_id"?}` |
| `POST /replay` | body `{"items": [{"header": <hex>, "certificate": <hex>}], "run_id"?}` |
| `GET  /tip` | 当前可信 tip |
| `GET  /headers/{digest}` | 按摘要索引取已接受头 |
| `GET  /audit?limit=` | 决策审计（接受/拒绝、错误码、关键中间状态） |

## 5. 运行测试

```bash
# 重新生成独立 golden 向量（正常无需；向量已入库）
.venv/bin/python tests/generate_golden.py

# 全量测试
.venv/bin/python -m pytest tests/ -v
```

测试覆盖要求中的每一类输入：

- **连续合法头**：`tests/test_legitimate_chain.py`（含一次委员会轮换、落盘重开续链、与 golden 摘要/签名/权重独立核对）；
- **权重不足（含恰好等于门槛的边界）**：`tests/test_reject_weight.py`；
- **旧委员会签新头 / 未预告换纪元 / 非法预告 / 授权不足的轮换**：`tests/test_reject_stale_committee.py`；
- **冲突头（同高度二义）/ 已知非 tip 父分叉 / 未知父 / 高度跳跃 / 轮次回退 / 异链**：`tests/test_reject_conflict.py`；
- **信任期边界（== 接受、+1s 需新检查点、长时间离线遇首个过期头即停）**：`tests/test_trust_boundary.py`；
- **检查点根保护（伪造签名/篡改报体/重复引导/未初始化拒绝头）**：`tests/test_bootstrap.py`；
- **离线回放（遇拒即停、preflight 资源耗尽、交叉绑定证书）**：`tests/test_replay.py`；
- **HTTP 状态码/错误信封分类（400/409/413/500 可区分）**：`tests/test_service.py`、`tests/test_error_taxonomy.py`；
- **拒绝后落盘状态不变（重开 SQLite 复核）**：`tests/test_store_atomic.py`；
- **编解码严格性（截断/拖尾/坏标签/跨类型串用/超限）**：`tests/test_codec.py`。

每个拒绝类测试都断言**具体 `ErrorCode`、`category`、`detail` 数值**，并比对
拒绝前后的可信状态（tip 摘要/高度 + 头表/委员会表集合）完全相等，而不是只看“接口能调用”。

## 6. 测试日志（可重放问题）

每次 pytest 运行生成唯一 `run_id`（UTC 时间 + pid + 随机后缀），
产物在 `tests/test-runs/<run_id>/`：

- `summary.log` / `summary.json`：每个用例的结果、错误码、错误类别、判断理由；
- `events.jsonl`：关键中间状态事件流。

内核还在 SQLite `audit` 表为**每一次**接受与拒绝留痕：`run_id`、
动作、结果、`error_code`/`error_category`、`stage`、`tip_before`、
`tip_after`、`state_unchanged`、时间戳。复现问题时：

1. 从 `events.jsonl` 或 `audit.detail.intermediate` 取失败头的高度/轮次/纪元/父摘要/权重；
2. 用相同 `run_id` 重放（fixtures 确定性，字节一致）；
3. `sqlite3 <db> "select result,error_code,detail from audit order by id"` 逐步对照。

离线回放制品见 `replay-artifacts/<run_id>/{stream.json,report.json,events.jsonl}`。

## 7. 复现步骤清单（验收对照）

```bash
.venv/bin/python -m pytest tests/ -q          # 全绿：76 passed
.venv/bin/python demo.py                      # 全部 OK，拒绝后 state_unchanged=True
.venv/bin/python demo_http.py                 # 200/409/400 分类正确
.venv/bin/python demo_replay.py              # applied=4/6, failure_index=4, WEIGHT_BELOW_QUORUM
```

## 8. 目录结构

```
lightclient/
  types.py  codec.py  crypto.py  errors.py  config.py
  store.py  kernel.py  replay.py  service.py  run.py
  fixtures/builder.py
tests/
  conftest.py  generate_golden.py  golden_vectors.json
  test_*.py
demo.py  demo_http.py  demo_replay.py  requirements.txt  README.md
```
