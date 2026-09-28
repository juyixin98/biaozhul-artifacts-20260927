# 本地审计批次：字段级承诺与选择性披露验证服务

在本地实现“先承诺、后选择性披露、第三方可验证”的审计工作流：提交一批
类型化记录后，服务为每个字段生成**加盐的域分离承诺**，聚合成公开的**批次
根**；之后可以只披露其中一个字段，任何持有批次根的一方都能用纯函数验证该
字段的真实值与**字段身份（路径/位置/记录）**，而无法接受被调包的字段。

全部数据为本地合成夹具，无生产账号、无外部服务。

* 技术栈：Python 3.10+ · FastAPI · SQLite · `cryptography`
* 协议版本：`audit-commit-v1`，服务版本：`1.0.0`
* 密码学细节、威胁模型与失败类别见 [`docs/PROTOCOL.md`](docs/PROTOCOL.md)

## 目录结构

```
app/
  config.py            独立配置层（环境变量/.env，启动时校验）
  core/                纯密码学内核（无 I/O、无框架，可独立复用）
    encoding.py        六类字段的规范化类型编码
    commitment.py      字段承诺（域分离标签 + 盐 + 路径/位置/类型/状态）
    merkle.py          两层带标签 Merkle 树（叶绑定 index/count）
    batch.py           批次构建、披露生成、纯函数验证（判定+失败类别+步骤）
    errors.py          稳定的失败类别枚举与分类异常
  parsing/             规则/证据解析边界 + 本地合成夹具
  security/            算法白名单、标签哈希、盐策略、低熵告警、脱敏扫描
  storage/             SQLite：batches / batch_secrets / audit_events 分表
  audit/               运行关联日志（run_id、版本、步骤、判定依据）
  service.py           用例编排（解析→内核→存储→审计）
  api/                 FastAPI 路由与严格 schema
independent/
  verifier.py          仅用标准库(hashlib)重写的独立验证器（参考答案第二来源）
tests/                 独立测试（74 个断言用例）
  golden/golden.json   确定性黄金向量（两实现共同背书）
scripts/
  generate_golden.py   生成黄金向量（确定性盐派生）
  demo.py              本地端到端演示，输出 docs/samples 请求/响应样例
docs/
  PROTOCOL.md          协议与安全边界
  samples/             实际程序产出的请求/响应样例
```

## 快速开始（从干净目录复现）

```bash
# 1) 创建虚拟环境并安装钉版本依赖
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

# 2) 配置（可直接使用默认值；复制样例可自定义）
cp .env.example .env        # 可选：修改 AUDIT_DB_PATH / 盐长 / 摘要算法

# 3) 运行全部测试（日志写入 test-results/<run-id>/logs/service.log）
python -m pytest

# 4) 重新生成黄金向量（确定性盐；两实现交叉验证）
python scripts/generate_golden.py

# 5) 运行本地端到端演示（生成 docs/samples 下的真实样例）
python scripts/demo.py

# 6) 启动 HTTP 服务
uvicorn app.main:app --host 127.0.0.1 --port 8077
```

## 依赖版本（requirements.txt）

| 包 | 版本 | 用途 |
|---|---|---|
| fastapi | 0.115.6 | HTTP 层 |
| uvicorn | 0.34.0 | ASGI 服务 |
| pydantic / pydantic-settings | 2.10.4 / 2.7.1 | schema 与配置 |
| cryptography | 44.0.0 | SHA-256/384/512、常量时间比较 |
| anyio | 4.6.2.post1 | starlette 测试客户端兼容版本 |
| httpx | 0.28.1 | 测试用 ASGI 传输 |
| pytest | 8.3.4 | 测试框架 |

## 配置项（前缀 `AUDIT_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUDIT_DB_PATH` | `./data/audit.db` | SQLite 文件；私有盐仅存于此 |
| `AUDIT_LOG_DIR` | `./logs` | 服务日志目录 |
| `AUDIT_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `AUDIT_DIGEST` | `sha256` | 白名单：sha256/sha384/sha512 |
| `AUDIT_DEFAULT_SALT_BYTES` | `16` | 字段盐长度（16–64） |
| `AUDIT_ALLOW_UNSALTED` | `false` | 是否允许无盐承诺（默认拒绝） |

## HTTP 接口与请求样例

所有写接口接受可选头 `X-Run-Id`（不提供则生成 `run-xxxxxxxx`），用于把日志
和审计事件关联到本次运行。

### `POST /api/v1/batches` — 提交承诺批次

请求样例：[`docs/samples/01-create-batch.request.json`](docs/samples/01-create-batch.request.json)

```bash
curl -s -X POST http://127.0.0.1:8077/api/v1/batches \
  -H 'Content-Type: application/json' -H 'X-Run-Id: demo-1' \
  -d @docs/samples/01-create-batch.request.json
```

响应只包含公开内容（每个字段的承诺、记录根、批次根、低熵告警），**不含盐
与原值**；样例见
[`docs/samples/02-create-batch.response.json`](docs/samples/02-create-batch.response.json)。
缺失字段（记录中省略的键）与显式 `{"state":"null"}` 都会被提交为不同状态。

### `POST /api/v1/disclose` — 披露单个字段

```bash
curl -s -X POST http://127.0.0.1:8077/api/v1/disclose \
  -H 'Content-Type: application/json' \
  -d '{"batch_id":"batch-synthetic-0001","record_index":0,"path":"subject.age"}'
```

输出见 [`docs/samples/04-disclose.response.json`](docs/samples/04-disclose.response.json)。
`reveal` 中只出现这一个字段的值和盐；其他字段的盐/值不在证明里。

### `POST /api/v1/verify` — 验证披露

验证方以**独立信任渠道**取得的批次根为锚，可额外固定
`expected_path` / `expected_record_index`：

```bash
curl -s -X POST http://127.0.0.1:8077/api/v1/verify \
  -H 'Content-Type: application/json' \
  -d @docs/samples/05-verify.request.json
```

成功响应（完整样例 [`06-verify.response.json`](docs/samples/06-verify.response.json)）：

```json
{
  "valid": true,
  "category": null,
  "checked_steps": ["...", "field_commitment_recomputed",
    "commitment_matches_claimed_leaf", "field_merkle_path_verified",
    "record_merkle_path_verified", "batch_root_matches_trusted_root"],
  "claim": {"path": "subject.age", "state": "present", "value": 29, ...}
}
```

密码学失败仍返回 HTTP 200（“验证动作”成功执行），但
`valid=false` 并给出**具体失败类别**；请求本身错误（未知批次/字段、非法
JSON）才返回 4xx。类别：

`PROOF_MALFORMED` · `TYPE_ENCODING_ERROR` · `COMMITMENT_MISMATCH` ·
`IDENTITY_MISMATCH` · `MERKLE_PATH_MISMATCH` · `ROOT_MISMATCH` ·
`FIELD_NOT_COMMITTED` / `RECORD_NOT_FOUND` / `BATCH_NOT_FOUND` ·
`POLICY_VIOLATION` · `INTERNAL_ERROR`（异常绝不折算为成功）

### 其他接口

* `GET /health` — 服务/协议版本；
* `GET /api/v1/batches`、`GET /api/v1/batches/{id}` — 公开列表/详情；
* `GET /api/v1/audit/events?run_id=...&batch_id=...` — 按运行/批次查审计。

## 测试如何满足验收要点

测试不满足于“接口可调用”，全部断言具体结果与失败类别：

| 验收点 | 测试 |
|---|---|
| 不同字段同值 → 承诺不同 | `test_same_value_different_fields_different_commitments` |
| 字段名/位置域分离 | `test_commitment_changes_when_path_changes` |
| 字段调换不能通过 | `test_swapped_field_identity_rejected`、交叉验证中的 `neighbour-leaf-commitment`/`wrong-record-request` |
| 空值 / null / 缺失 | `test_empty_string_present_is_distinct_from_null_and_missing`、`test_valid_proofs_for_present_null_and_missing`、`test_missing_proof_must_carry_no_salt_or_value` |
| 错误盐 / 错误值 | `test_wrong_salt_is_commitment_mismatch`、`test_wrong_value_is_commitment_mismatch` |
| 错误根 | `test_wrong_root_is_root_mismatch`、`test_golden_proof_foreign_root_fails_in_both` |
| 兄弟路径/记录根伪造 | `test_tampered_sibling_is_merkle_mismatch`、`test_forging_record_root_fails_merkle_step` |
| 独立验证器不能接受身份替换 | `tests/test_independent_crosscheck.py` 全部变异要求两实现类别一致 |
| 参考答案非被测实现自产 | `independent/verifier.py` 仅用 hashlib；`tests/test_golden_vectors.py` 重放黄金向量并独立重算全部承诺与根 |
| 盐/值不进公开证明与审计 | `test_public_batch_payload_has_no_salts`、`test_audit_trail_is_run_correlated_and_redacted` |
| 低熵枚举限制 | `test_low_entropy_unsalted_is_enumerable_salted_is_not` |
| 异常/未知不返回成功 | `verify_proof` 兜底分支 + 所有失败用例断言 `valid is False` 与类别 |

### 日志可关联性与判定依据

每条服务日志包含服务/协议版本、`run_id`、输入指纹和计算步骤，例如：

```
... INFO  svc=1.0.0 proto=audit-commit-v1 run=smoke-run-1 input=- create_batch:committed {'batch_root': '...'}
... WARNING svc=1.0.0 proto=audit-commit-v1 run=smoke-run-2 input=- verdict REJECT category=ROOT_MISMATCH reason=...
```

测试会话自身的日志位于 `test-results/<test-run-id>/logs/service.log`，会话 id
记录在同目录 `session.json`；审计事件可用 `GET /api/v1/audit/events?run_id=`
按运行检索。

## 已执行的验证结果

以下结果在本目录实际执行（Python 3.12.3，Linux x86_64）。为验证“干净目录
复现”，曾删除 `.venv` 后新建隔离环境，用 `requirements.txt` 重新安装：

* 全新 venv `pip install -r requirements.txt` — 安装成功；
* `python scripts/generate_golden.py` — 批次根
  `2ee2d86a5abf6b231a0c574b1b3193e59676f322289d967ffc49107e1201f2a1`，
  独立验证器 `valid=True`（黄金根确定性，可重复复现）；
* `python scripts/demo.py` — 服务与独立验证器均验证通过，样例落盘
  （随机盐批次，每次根不同，属预期）；
* `uvicorn` + curl 冒烟：创建/披露/错误根验证（`ROOT_MISMATCH`）/审计查询
  行为与文档一致；
* 额外边界手测：跨批次重放 → `ROOT_MISMATCH`；独立验证器收到非法 hex →
  `PROOF_MALFORMED`；
* 干净环境全量测试（`pytest.ini` 中 `filterwarnings = error`，告警即失败）：

```
$ python -m pytest
collected 74 items

tests/test_api.py ..........                                  [ 13%]
tests/test_commitment_acceptance.py .............................  [ 51%]
tests/test_golden_vectors.py ....                             [ 56%]
tests/test_independent_crosscheck.py ...                      [ 60%]
tests/test_merkle.py ................                         [ 82%]
tests/test_parsing.py ......                                  [ 90%]
tests/test_security_boundary.py ......                       [100%]
============================== 74 passed in 0.86s ==============================
```

## 安全边界备忘

* 盐最小 16 字节、OS CSPRNG；无盐承诺默认拒绝；
* 公开接口与审计载荷不包含盐或未披露原值（有扫描器测试兜底）；
* 比较使用 `cryptography` 的常量时间比较；
* 低熵字段即使加盐也不保证盐泄露后的不可枚举性——已在协议文档与批次告警中
  明确声明。
