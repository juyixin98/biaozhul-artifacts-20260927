# 本地响应元数据缓存键与 Vary 配置审计后端

对本地响应元数据（请求/响应证据）做缓存键派生与 `Vary` 配置审计的后端服务：

- 按**声明的请求维度**判断哪些响应可以共享；
- `Authorization` / `Cookie` 有专门的身份规则，**不允许隐式跨身份复用**；
- 严格区分响应 `Vary` 的**通配 `*`** 与**完全缺失**；
- 只判断“给定策略是否覆盖差异”，**不声称自动理解任意业务隐私**；
- 产出可复现的**碰撞请求对见证**，并提供“修复键 → 碰撞消失”的核验。

技术栈：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`）· `cryptography`（Fernet + HMAC-SHA256）。
所有数据、身份令牌、外部参与者均为**本地合成夹具**，无需任何生产账号。

---

## 1. 验收反例（可核验结果）

夹具：[`fixtures/scenarios.json`](fixtures/scenarios.json)（期望为人工编写，不由被测内核生成）。

| 场景 | 请求差异维度 | 缺陷策略下的结果 | 原因码 | 严重级 |
|---|---|---|---|---|
| S1 语言协商 | `Accept-Language`，响应声明 `Vary: Accept-Language` 被忽略 | 1 个碰撞见证 | `response_vary_declared_but_policy_ignored` | medium |
| S2 压缩编码 | `Accept-Encoding: identity/gzip`，原始字节不同 | 1 个碰撞见证 | `response_vary_declared_but_policy_ignored` | medium |
| S3 身份（Authorization） | 两个合成用户的 Bearer 令牌不同 | 1 个碰撞见证 | `identity_authorization_cross_identity` | **critical** |
| S4 私有缓存（Cookie） | 两个会话 Cookie 不同 | **0**（私有键隐式绑定身份） | — | — |
| S5 `Vary: *` | 响应 `Vary: *` 被策略忽略，`Accept` 不同 | 1 个碰撞见证 | `response_vary_wildcard_ignored` | high |
| S6 Vary 缺失 | 响应**没有任何** `Vary` 头，语言仍被错误共享 | 1 个碰撞见证 | `vary_absent_and_policy_missing_dimension` | medium |
| S7 阴性对照 | 语言不同但响应字节完全相同 | **0**（不同请求≠碰撞，防止误报） | — | — |
| S8 共享缓存 Cookie | 共享缓存中两个会话 Cookie 不同却同键 | 1 个碰撞见证 | `identity_cookie_cross_identity` | **critical** |

修复键（`/remediate`）做三件事：尊重响应 `Vary`（含通配禁存语义）、把 `Authorization`/`Cookie`
显式纳入共享缓存键、把“源站缺失 Vary”时实际见证到的差异维度补进键。
修复后对**同一批证据**重放：S1/S2/S3/S5/S6/S8 的发现数全部变为 0，`collision_gone=true`，
且被清除的见证 id 与残留见证 id 都会返回。

### 参考答案的独立性

- 每个场景的 `expected_broken` / `expected_fixed`（维度、原因码、严重级、证据对）是**手写**在夹具里的；
- 另有一份与生产内核完全分开书写的朴素预言机 [`tests/oracle.py`](tests/oracle.py)，
  用独立实现的键分组确认“手写期望的那对请求在缺陷策略下确实同键、在修复策略下确实分离”；
- 被测内核不生成任何期望值。

---

## 2. 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 跑测试（32 个，必须实际执行）
.venv/bin/python -m pytest

# 一键端到端演示（自动起真实 HTTP 服务、提交夹具、出证、修复、核验、校验哈希链）
.venv/bin/python scripts/demo.py

# 启动服务
AUDIT_MASTER_KEY="$(.venv/bin/python -c 'import secrets,base64;print(base64.b64encode(bytes(range(32))).decode())')" \
  .venv/bin/python run_server.py
# 默认监听 127.0.0.1:8000，可用 AUDIT_HOST/AUDIT_PORT 覆盖
```

打开 <http://127.0.0.1:8000/docs> 可交互调试。

### 手工复现一次碰撞

```bash
RUN=manual-$(date +%s)
curl -s -X POST localhost:8000/v1/runs -H 'content-type: application/json' \
  -d "{\"run_id\":\"$RUN\"}"
curl -s -X PUT localhost:8000/v1/runs/$RUN/policy -H 'content-type: application/json' \
  -d @fixtures/manual_broken_policy.json
curl -s -X POST localhost:8000/v1/runs/$RUN/evidence -H 'content-type: application/json' \
  -d @fixtures/manual_evidence.json
curl -s -X POST localhost:8000/v1/runs/$RUN/analyze | python -m json.tool
curl -s -X POST localhost:8000/v1/runs/$RUN/remediate | python -m json.tool
curl -s localhost:8000/v1/runs/$RUN/verify | python -m json.tool
```

---

## 3. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/runs` | 创建审计运行（可指定 `run_id`，用于重放） |
| GET | `/v1/runs` / `/v1/runs/{id}` | 列表 / 详情（`status`、证据数、是否已分析） |
| POST | `/v1/runs/{id}/seal` | 封口（封口后只读） |
| PUT | `/v1/runs/{id}/policy` | 配置被审计策略（**有证据后锁定**） |
| GET | `/v1/runs/{id}/policy` | 读取策略 |
| POST | `/v1/runs/{id}/evidence` | 批量提交证据（整批原子；响应体需带 `body_sha256`） |
| GET | `/v1/runs/{id}/evidence` | 列出证据（密文字段解密返回） |
| POST | `/v1/runs/{id}/analyze` | 分析：缓存性决策、生效 Vary、派生键、碰撞见证 |
| GET | `/v1/runs/{id}/analysis` | 取最近分析结果 |
| POST | `/v1/runs/{id}/remediate` | 构造修复键策略并重放，返回 `before/after/cleared/residual` |
| GET | `/v1/runs/{id}/events` | 审计事件链（运行编号、类型、时间、载荷、前后哈希） |
| GET | `/v1/runs/{id}/verify` | 重算 HMAC 哈希链，报告首个不匹配位置 |

分析结果中的关键中间状态：

- `cacheable_decisions`：每条证据是否允许进入缓存；
- `response_vary_state`：每条响应的 Vary 状态（`absent` / `wildcard` / `explicit`）；
- `effective_vary`：策略声明 ∪ 响应声明后实际生效的维度；
- `derived_keys`：每条证据的键分量（`vary` / `identity` 及身份来源）；
- `decision_rationale`：逐条**判断理由**（入审计日志）。

### 策略字段语义

| 字段 | 含义 |
|---|---|
| `cache_scope` | `shared`（共享）/ `private`（私有）。私有键隐式绑定身份 |
| `vary_headers` | 策略声明纳入键的请求头；**不允许 `*`**（通配是响应 Vary 的特殊语义） |
| `include_authorization` / `include_cookie` | 共享缓存是否把身份头纳入键 |
| `allow_storing_authorization_response` | 共享缓存是否允许存带 `Authorization` 的响应（默认否） |
| `allow_storing_cookie_response` | 共享缓存是否允许存带 `Set-Cookie` 的响应（默认否） |
| `respect_response_vary` | 是否采信响应自身的 `Vary`（修复键的关键开关） |
| `vary_wildcard_mode` | 对 `Vary: *` 的策略：`forbid` 禁存（默认）/ `uncacheable` 不缓存放行 |

---

## 4. 错误语义（四类失败可区分）

所有错误统一形如：

```json
{"error": {"category": "...", "code": "...", "message": "...", "details": {...}}}
```

| category | HTTP | code | 触发情形 |
|---|---|---|---|
| `input_error` | 422 | `run_id_invalid` | run_id 字符集/长度非法 |
| | 422 | `evidence_invalid` | 证据 id 重复、字段缺失、base64 非法 |
| | 422 | `policy_invalid` / `request_validation` | 策略自相矛盾（如 Vary 含 `*`）/ 请求体校验失败 |
| | 422 | `pagination_invalid` | limit/offset 越界 |
| `state_conflict` | 409 | `run_exists` | run_id 已被占用 |
| | **404** | `run_not_found` | 运行不存在（类别仍是 state_conflict，HTTP 用 404，不泄漏内容） |
| | 409 | `policy_not_set` | 未配置策略就提交证据/分析；或读取不存在的分析 |
| | 409 | `policy_locked` | 已有证据后试图改策略 |
| | 409 | `run_not_open` | 封口后写入 / 重复封口 |
| | 409 | `nothing_to_analyze` | 零证据执行分析 |
| `resource_exhausted` | 507 | `too_many_evidence` | 每运行证据条数超限（默认 200） |
| | 507 | `evidence_too_large` | 单条响应体超限（默认 64 KiB） |
| `computation_failure` | 422 | `body_hash_mismatch` | 声明的 `body_sha256` 与实算不符（含截断/篡改） |
| | 422 | `ciphertext_invalid` | 主密钥不匹配或密文损坏 |
| | 422 | `chain_verification_failed` | 哈希链核验不通过（由 `/verify` 体现） |
| | 422 | `key_derivation_failed` | 主密钥派生失败 |

写入操作是**整批原子**的：批次内任一条证据触发摘要不符/超限/重复 id，整批回滚，不留半截状态。

---

## 5. 模块与数据/错误契约

```
app/
  errors.py    统一错误类别与 code → HTTP 状态映射
  models.py    Pydantic 数据契约（Policy / Evidence / AnalysisView / WitnessPair …）
  parser.py    规则/证据解析：头部归一化、Vary 三态、Cache-Control、query 规范化、摘要校验
  kernel.py    安全内核：缓存性判定、键派生、身份隔离、碰撞见证、修复键重放（纯函数，无 IO）
  crypto.py    Fernet 字段加密 + HMAC-SHA256 事件链；HKDF 按 run_id 派生子密钥
  storage.py   SQLite：run 状态机、证据加密落盘、事件链、分析结果；线程锁串行化
  api.py       FastAPI 路由与编排，统一错误信封
fixtures/      合成场景夹具 + 手工复现请求
scripts/demo.py 端到端演示（真实 HTTP）
tests/         32 个测试 + 独立朴素预言机
```

契约边界：

- `parser` 只做声明式文本解析，不做隐私判断；任何摘要不符抛 `computation_failure`。
- `kernel` 是纯函数：入参 `Policy + list[Evidence]`，出参固定结构（含 `findings[].pair` 见证）；
  不碰数据库与网络。
- `storage` 负责状态机与错误类别：不存在/已封口/策略锁定均为 `state_conflict`，
  超限为 `resource_exhausted`。
- `api` 只编排：调用方拿到的错误信封在所有路径上结构一致。

---

## 6. 安全设计要点

- **存储加密**：`Authorization`、`Cookie`、响应体以每运行派生的 Fernet 密钥加密落盘；
  SQLite 文件中不出现明文令牌（测试 `test_secrets_encrypted_at_rest` 直接扫描库文件断言）。
- **密钥隔离**：`HKDF(主密钥, salt=run_id)` 派生加密密钥与链密钥；换主密钥无法解密旧库（可区分的 `ciphertext_invalid`）。
- **防篡改日志**：每个事件 `entry_hash = HMAC(链密钥, seq|type|ts|payload|prev_hash)`，
  逐条链接自创世哈希；`/verify` 重算并报告首个不匹配序号。测试包含直接改库的篡改用例。
- **身份规则**：
  - 共享缓存默认**拒存**带 `Authorization` / `Set-Cookie` 的响应（除非策略显式放行）；
  - 一旦放行且未把身份纳入键，同键跨身份一律判 `critical`，与响应体是否不同无关（若相同则说明共享本身无害——但键仍然分离）；
  - 私有缓存的键**始终**隐式绑定 `Authorization`/`Cookie`（来源标记 `private_implicit`）。
- **受限立场**：内核只按声明的维度与显式身份头判定，不推断业务语义；
  源站未声明 Vary 的维度，只在证据中实际观察到差异时作为修复建议补入键。

---

## 7. 测试日志与重放

- `pytest` 会写 `data/test-runs/pytest-<时间戳>.jsonl`，每行含：测试名、阶段序号、
  运行 `run_id`、关键中间状态（发现列表/理由/链核验结果）。
- 演示写 `data/demo_last_run.jsonl`，并打印可重放的 run_id 与启动命令。
- 重放需要同一 `AUDIT_MASTER_KEY`（否则密文不可解、链也无法核验）。

运行测试的实际结果（本机执行）：

```
$ .venv/bin/python -m pytest
32 passed
```
