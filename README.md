# logsafe — 按字段/模式规则脱敏、保留可审计原文位置映射的日志服务

本地合成数据（synthetic fixtures）实现，Python 3.12 + FastAPI + SQLite +
`cryptography`。无任何生产账号或真实业务数据依赖。

## 1. 本地验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 一键验证：pytest + 启动真实 HTTP 服务冒烟 + 服务日志泄漏 grep
./scripts/verify_local.sh

# 只跑单元/集成测试
.venv/bin/python -m pytest -q

# 手动启动
.venv/bin/python run_server.py        # http://127.0.0.1:8088/docs
```

**通过判据**（`verify_local.sh` 末尾打印 `VERIFY: PASS` 需三者同时成立）：

1. `pytest_rc=0`：56 个测试全部通过。测试断言具体输出与失败类别
   （`unknown_profile` / `profile_conflict` / `validation_error` /
   `audit_access_denied` / `session_not_found` / `rule_compile_error` /
   `audit_integrity_error`），不是“接口可调”级检查。
2. `smoke_rc=0`：真实 HTTP（urllib，非进程内调用）9 组检查全部 PASS，
   覆盖跨块令牌、转义 JSON、重复片段、相邻/重叠规则、流式==整段、规则切换、
   证据不足单列、审计鉴权与 reveal、哈希链。
3. `log_leak=0`：`grep` 服务自身日志，四个合成敏感值（邮箱/令牌/卡号/身份证）
   均不出现。

审计接口需要请求头 `X-Audit-Key: local-synthetic-audit-key`
（可用 `LOGSAFE_AUDIT_KEY` 覆盖）。

## 2. 模块关系与职责

| 模块 | 职责 |
|---|---|
| `app/rules.py` | **规则/证据解析**：`pattern` 与 `field` 两类规则；内置 `standard` / `strict` 两个版本化规则集；证据校验器（Luhn、身份证校验位）；规则指纹（规则内容 SHA-256 前 16 位） |
| `app/kernel.py` | **安全内核**：候选分类（确定/不确定）、优先级重叠仲裁、偏移映射构建、流式状态机。整段 `redact_full()` 与流式 `StreamRedactor` 共用同一仲裁与渲染逻辑 |
| `app/crypto.py` | 原文的认证加密（Fernet = AES-128-CBC+HMAC），解密时校验 SHA-256；审计事件哈希链原语 |
| `app/audit.py` | **审计接口（存储侧）**：SQLite 三张表（requests / fragments / events），原文仅以密文落盘；append-only SHA-256 哈希链；鉴权读取与 reveal |
| `app/state.py` | **状态隔离**：每会话独立 redactor/缓冲区/计数；会话钉在创建时的规则指纹上，中途换 profile 返回 `profile_conflict` |
| `app/api.py` | FastAPI 装配：请求身份（`request_id`，支持 `X-Request-Id` 透传并校验字符集）、错误信封、安全日志、所有路由 |
| `app/logging_utils.py` | 服务自身日志：**字段允许名单** + 按本请求敏感子串二次擦洗 |
| `app/schemas.py` / `app/config.py` | Pydantic 模型 / 本地配置（DB 路径、Fernet key、审计 key） |
| `tests/` | `test_kernel.py`（内核具体结果）、`test_api.py`（接口/审计/隔离）、**`test_independent_oracle.py`（独立预言机，不导入内核正则）**、`test_log_safety.py`（日志不泄漏）、`synth_fixtures.py`（合成夹具） |

## 3. 关键算法假设

1. **偏移单位 = Unicode 码点（Python `str` 索引）**，不是 UTF-8 字节。
   映射同时给出原文区间 `[original_start, original_end)` 与输出区间
   `[output_start, output_end)`，长度变化（替换符与原文不等长）由此显式表达；
   `output_length = input_length + Σ(replaced_length − original_length)`。
2. **重叠仲裁确定化**：优先级高者胜；同优先级按起点早→跨度大→rule_id
   排序，结果与 dict 顺序无关。仅“相交”才冲突，相邻
   （`a.end == b.start`）两个秘密都会被脱敏。`field`（40/38）>
   `api_token`(30) > `bearer`(29) > `jwt`(28) > `secret_hex`(26) >
   `bank_card`(24) > `cn_id`(23) > `cn_mobile`(22) > `email`(20)。
3. **跨块尾片段不提前放行**：流式时始终保留尾部 `max_len`（当前规则集
   600 字符）不输出；切点若被某个候选跨过，则继续左移到该候选起点。
   任何在“未来一块里可能变成更长匹配”的位置都不会先输出。等价保证：
   各块 `emitted` 拼接 + flush == 同一输入的整段结果（测试对 cut=1..19
   全部参数化验证）。
4. **证据校验失败 ≠ 直接放行**：正则形似但 Luhn/身份证校验位不过的片段
   不做确定性脱敏，而是进入单独的 `uncertain` 列表（标签带 `?`，附
   `reason`，只给位置、长度、SHA-256，不回显原文）。另有两条最终启发式：
   校验位失败的 13–19 位数字串、过短的 `sk_/tok_` 前缀（疑似截断令牌）。
5. **脱敏后无残留**：field 规则只替换 value 子区间，保留键与引号
   （含 `\"` 转义形态），所以 JSON 套 JSON 的日志脱敏后仍可 `json.loads`
   两次；测试断言输出中不存在原文及长度 ≥6 的前/后缀。
6. **边界**：卡号/手机号/身份证用数字环视而非纯 `\b`，因此
   `邮箱+手机号` 相邻时不会被 `\b` 粘连漏匹配；但与前导字母数字完全粘连、
   无任何分隔的令牌（如 `xxxxsk_...`）不视为令牌——这是有意的保守边界
   假设（结构化 `token=` 形态由 field 规则兜底）。
7. **规则版本**：`standard-v1` / `strict-v1` 固定；规则集指纹由规则 id、
   正则、优先级、校验器规范序列化后哈希，规则内容变指纹就变。流式会话
   以创建时指纹为准（“规则切换”在会话之间生效，不允许会话中途热切换）。
8. **审计**：原文字段在 SQLite 中仅存 Fernet 密文 + SHA-256；
   列表接口默认不返回原文，`?reveal=true` 且审计 key 正确才解密；
   事件表为哈希链（前一条 entry_hash 入下一条），改任意一行 payload
   `GET /api/v1/audit/chain` 即报 `ok:false`（有篡改检测测试）。
9. 服务自身日志只允许结构字段，自由文本经“本请求敏感子串列表”擦洗；
   错误响应不回显提交值（校验错误只给字段路径）。

## 4. HTTP 接口

- `GET /health` — 版本 + 哈希链状态
- `GET /api/v1/rules` — 规则集版本、指纹、每条规则的优先级/校验器
- `POST /api/v1/redact` — 整段脱敏（body: `text`, `profile?`）
- `POST /api/v1/sessions` / `POST /api/v1/sessions/chunk` — 流式
  （chunk + `final`；响应含 `emitted`、本块新映射、累计 input/output 长度）
- `GET /api/v1/audit/requests[?limit]`、
  `GET /api/v1/audit/requests/{rid}[?reveal=true]`、
  `GET /api/v1/audit/chain` — 需 `X-Audit-Key`

每个响应/错误信封都带 `request_id`（响应头 `X-Request-Id` 同名）。

## 5. 依赖版本（本地验证通过）

Python 3.12.3；fastapi 0.115.14、uvicorn 0.32.1、pydantic 2.9.2、
starlette 0.46.2、cryptography 41.0.7、pytest 8.3.5、httpx 0.27.2
（TestClient 传输）。数据全部为 `tests/synth_fixtures.py` 本地构造，
卡号/身份证由算法补合法校验位，无真实值。
