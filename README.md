# 日志脱敏服务（log-redaction-service）

按**字段规则**与**模式规则**对流式/整段日志脱敏，保留**可审计的原文位置映射**，
原文加密存储。全部使用本地**合成数据**，无任何真实业务数据或外部账号依赖。

技术栈：Python 3.12 · FastAPI · SQLite(WAL) · `cryptography`(Fernet) · pytest。

---

## 1. 快速开始

```bash
cd /home/admin/Downloads/xinbiaozhu/opp271/b
bash run_checks.sh                 # 建虚拟环境 + 装依赖 + 跑全部测试
```

手动启动服务：

```bash
source .venv/bin/activate
python -m app.main                 # http://127.0.0.1:8080
# 或
uvicorn app.api.main:app --host 127.0.0.1 --port 8080
```

冒烟调用（合成数据）：

```bash
curl -s http://127.0.0.1:8080/api/v1/healthz
curl -s -X POST http://127.0.0.1:8080/api/v1/redact \
  -H 'Content-Type: application/json' \
  -d '{"text":"card 6225123456789010 phone 19900001111"}'
```

---

## 2. 模块关系（多模块后端，核心无硬编码演示）

```
config/rules.json        规则档（standard / strict）：模式正则、字段键集合、
                         优先级、prefix_hint、tail_suspicion、尾部策略
app/
  config.py              环境变量配置（DB 路径、Fernet 密钥、审计令牌、长度上限）
  rules/
    models.py            PatternRule / FieldRule / Profile 值对象
    parser.py            规则档加载与启动期校验（坏正则/未锚定/重复 id 即拒绝启动）
  core/                  安全内核
    json_lex.py          增量 JSON 结构词法器（跨 chunk、转义、字段证据、safe_pos）
    redactor.py          流式脱敏器：缓冲/边界、候选收集、重叠裁决、尾部歧义、
                         残留自检、双向位置映射
  state/
    sessions.py          请求级状态隔离（一请求一 redactor；request_id 关联）
    audit_store.py       SQLite + Fernet：原文加密落库，映射/步骤/不确定单列
    logging_utils.py     不含原文的安全日志（记录在到达 handler 前过滤秘密）
  audit/__init__.py      审计服务：元数据（公开）/ 详情与原文（令牌、恒定时间比较）
  services/
    redaction_service.py 编排：规则档→内核→会话→审计落库；错误分类
  api/
    schemas.py           Pydantic 模型
    main.py              FastAPI 路由与可解释响应
tests/
  fixtures.py            合成夹具（卡号/手机/令牌/工号/邮箱/密码，全部虚构）
  oracle.py              独立参考实现（不 import 被测 core，整段、不同实现路径）
  test_*.py             66 个断言具体结果与失败类别的用例
```

数据流：

```
输入块 ─► StreamingJsonLexer(字段证据/safe_pos)
       └► StreamingRedactor：保留尾部缓冲 + prefix_hint + 匹配回拉
             ─► 候选(模式+字段) ─► resolve_overlaps(优先级/包含/确定性次序)
             ─► 替换 + 映射(原文偏移↔输出偏移, sha256, 加密原文)
             ─► 残留自检（模式在输出上零命中）
             ─► AuditStore(SQLite/Fernet) ；事件/失败/不确定单列
```

---

## 3. 核心算法假设与保证

### 3.1 跨块识别：未完整识别的尾片段绝不提前放行

- 内核始终保留一个长度为最长模式长度（`max_length`，本档 254=邮箱）的尾部缓冲，
  以及 1 个字符的**边界回看**，用于在缓冲起点正确求值 `\b`/后顾断言
  （冲刷不会丢掉秘密的前导边界字符——这是专门修复过的跨块 bug）。
- 每个规则带 `prefix_hint`（锚定 `$`）：只要保留尾部的某个尾部还"像"该规则
  某完整匹配的前缀，边界就收紧到该位置之前；同时词法器 `safe_pos`/`held_from`
  对未闭合的键、敏感值起点精确扣留。
- `_pull_back_across_matches` / `_settle_closed_fields` 保证边界既不切过
  已完整到达的模式匹配，也不切开刚闭合但很长的字段值。
- EOF 处若仍存在"像某规则不完整前缀"的 token，按规则档策略处理：
  `standard=mark`（单列 `AMBIGUOUS_TAIL` 不确定结论），
  `strict=error`（`status=error, error_code=AMBIGUOUS_TAIL`，要求补齐/确认）。
  **绝不在证据不足时静默放行或伪装成成功。**

### 3.2 优先级与重叠

候选排序键（小者优先）：`priority → 起点 → 长度(降序) → 字段来源 → rule_id`，
然后贪心选择互不重叠候选。相邻（端点相接）不算重叠，各自保留。被压制候选通过
`rejected` 与 `OVERLAP_REJECTED` 事件逐条留痕（`OVERLAP_PRIORITY` /
`OVERLAP_CONTAINED`）。完全平局由 `rule_id` 字典序决定，结果确定可复现。
字段证据（`priority=10`）比模式（20）更具体，优先覆盖整个值区间（含引号）。

### 3.3 替换后无部分原值残留

- 每条映射含原文 sha256、`[original_start, original_end)` 与
  `[output_start, output_end)`；长度变化经输出偏移精确记录。
- `_verify_no_residuals`：① 每个入选候选原文不得在输出中出现；
  ② 每条模式规则在**最终输出**上必须零命中。任一失败 → `status=error`、
  `error_code=RESIDUAL_SECRET_DETECTED`，自检发现的问题不静默。
- JSON 词法器遇到非法转义/结构损坏：报 `INVALID_ESCAPE` /
  `STRUCTURAL_INVALID` 并**停用字段识别**（降级为仅模式扫描），不猜测放行；
  未闭合敏感字符串在 EOF 已见内容仍会被替换并标记 `UNCLOSED_STRING`。

### 3.4 流式与整段同一路径

`redact_whole` = 单块 `feed` 后 `finalize`。66 个用例（含 12 种固定切分、
全部 32 个令牌切分点、转义符/unicode 跨块、100 组随机切分）断言流式结果与
整段逐字符一致，且映射区间一致。

### 3.5 已知边界（明确的假设）

- 模式以字符（code point）偏移计；`prefix_hint`/`tail_suspicion` 是声明式近似，
  倾向"宁可多扣、不可泄漏"。
- 熔成单一字母数字 token 的两个秘密（如 `"6225…010EMP…"`，中间无任何边界）
  无法同时满足两侧 token 边界，属于规则固有局限；残留自检会暴露此类未覆盖秘密。
- 审计库的对称密钥通过 `LOG_REDACT_AUDIT_KEY` 注入；仓库内默认值仅用于本地
  合成数据，生产必须更换。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/redact` | 整段脱敏，返回完整结果、映射、失败、不确定、步骤 |
| POST | `/api/v1/stream/open` | 打开流式会话，返回 `request_id` |
| POST | `/api/v1/stream/chunk` | 发送块（`final:true` 收尾），返回可安全发出的增量 |
| POST | `/api/v1/stream/finalize` | 显式收尾（重复收尾 → 409 `SESSION_CLOSED`） |
| GET  | `/api/v1/healthz` | 引擎版本、规则档、活跃会话数 |
| GET  | `/api/v1/requests/{id}` | 公开元数据（无输出/原文） |
| GET  | `/api/v1/audit/requests/{id}` | **需 `X-Audit-Token`**：输出、映射、步骤、不确定 |
| GET  | `/api/v1/audit/requests/{id}/mappings/{i}/original` | **令牌**：单条加密原文 + 完整性哈希 |

失败分类（`error_code`，非仅"接口可调用"）：`UNKNOWN_PROFILE`、
`SESSION_NOT_FOUND`、`SESSION_CLOSED`、`INPUT_TOO_LARGE`、
`AMBIGUOUS_TAIL`、`RESIDUAL_SECRET_DETECTED`、`AUDIT_TOKEN_MISSING`、
`AUDIT_TOKEN_INVALID`、`MAPPING_INDEX_OUT_OF_RANGE`。

响应可解释性：每条结果都带 `request_id`、`profile`/`profile_version`、
`engine_version`、长度与各规则命中位置；`steps` 给出
`CHUNK_RECEIVED/CHUNK_EMITTED/OVERLAP_REJECTED/UNCERTAINTY/RESIDUAL_FOUND/
FINALIZED` 关键步骤（只含偏移/计数，不含原文）；失败原因与不确定结论在
`error_*` 与 `uncertainties` 中单列。

---

## 5. 本地验证命令与预期判断

```bash
source .venv/bin/activate
python -m pytest -v                 # 预期：66 passed
```

覆盖矩阵与"通过判据"：

- **跨块令牌**：`test_token_split_at_every_position` —— 32 个切点逐一，
  任一切点都不得在收尾前发出令牌；最终 == `lead <TOKEN> tail`。
- **转义 JSON**：`test_escape_split_across_chunks`、
  `test_unicode_escape_split_across_chunks`、`test_string_field_with_*` ——
  `\"`、`\\`、`中` 跨块仍正确界定，字段整体替换、内部引号不外泄。
- **重复片段**：`test_no_raw_secret_residual_anywhere`、随机大批量重复
  （手动脚本）—— 每个秘密在输出中零出现，`residual_findings==[]`。
- **相邻规则**：`test_adjacent_rules_both_fire`、`test_overlaps.py` ——
  `","` 相邻两规则各自命中、`rejected==[]`；重叠时优先级/包含/平局确定。
- **流式==整段**：`test_streaming_equals_whole_fixed_sizes[*]`、
  `test_streaming_split_points_in_secret`、`test_whole_matches_independent_oracle_standard`
  —— 与**独立 oracle** 逐字符、逐区间一致。
- **规则切换**：`test_profile_switch_changes_field_keys`、
  `test_truncated_card_errors_in_strict` —— strict 扩展敏感键、尾部按 error。
- **日志不泄漏原文**：`test_logs_contain_no_secret_text`（下游采集 handler
  看不到明文，丢弃计数 +1）、`test_sqlite_file_contains_no_plaintext_secret`
  （物理库文件不含明文秘密）。
- **独立断言**：oracle（`tests/oracle.py`）不 import 任何 `app.core` 模块，
  期望输出来自独立实现，而非被测核心自身生成。

---

## 6. 依赖版本（已锁定 / 实测）

| 包 | 版本 |
|---|---|
| Python | 3.12.3 |
| fastapi | 0.141.1 |
| uvicorn[standard] | 0.54.0 |
| pydantic | 2.13.5 |
| starlette | 1.7.0 |
| cryptography | 50.0.1 |
| httpx | 0.28.1 |
| pytest | 9.1.1 |

环境变量（均有本地默认值）：`LOG_REDACT_RULES_PATH`、`LOG_REDACT_DB_PATH`、
`LOG_REDACT_AUDIT_KEY`（Fernet）、`LOG_REDACT_AUDIT_TOKEN`、
`LOG_REDACT_MAX_CHARS`。

---

## 7. 测试状态

- **已运行并通过**：66/66（`python -m pytest -q` → `66 passed`）。
- 另有未计入 pytest 的**手动对抗验证**：100 组随机切分（0 失败）、空输入/纯
  空白/多字节 emoji、5×重复秘密零残留、strict 尾部错误语义、审计令牌 401/403。
- **未运行**：无（如需 Windows/macOS 与真实 ASGI 多 worker 压测，当前环境
  仅在 Linux 单进程 TestClient/uvicorn 验证）。
