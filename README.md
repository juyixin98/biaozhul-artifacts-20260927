# osdiff — 对象存储策略离线差分分析

在一个**有限、可穷举的请求空间**上，离线比较同一套对象存储授权策略的两个版本，回答：

> 新版本是否扩大了“可访问请求集合”？是确定扩大，还是因为条件含未知值而“可能扩大”？

全程使用本地合成夹具与本地 Ed25519/SQLite 依赖，**无任何生产账号或真实业务数据**，也**不构建通用用户/角色后台**——principal 只是不透明字符串。

---

## 1. 它如何防止“正常输入看着对、边界输入悄悄算错”

| 要求 | 落实方式 |
|---|---|
| **显式拒绝优先 + 默认拒绝** | 内核判定顺序：命中的 `Deny` > 命中的 `Allow` > 无命中默认 `DENY_NO_MATCH`。显式 Deny 即使与 Allow 同时命中也优先（`osdiff/kernel.py`）。 |
| **未知条件值 → 不确定，绝不默认允许** | 三值逻辑（Kleene）：条件引用未知值时该语句为 *indeterminate*，整体裁决为 `UNKNOWN`，单独成类，既不许成 ALLOW 也不武断 DENY；只有 `...IfExists` 算子的“属性缺失”才按策略显式假设为真。 |
| **比较是否扩大可访问集合** | 对每个受限请求求 `(旧裁决, 新裁决)` 迁移类别：`EXPANSION_PROVEN`（确定扩大）、`EXPANSION_POSSIBLE`（过去确定拒绝→现在 UNKNOWN）、`CONTRACTION`、`DENY_TIGHTENED` 等。 |
| **输出新增允许/拒绝的具体请求见证** | 每类变首都输出具体 witness（principal/action/resource/attributes + 两版真实裁决与逐步 trace），按请求身份哈希确定序、每类限量，**计数始终完整**，截断会显式标记。 |
| **见证必须在两版本真实复核** | 引擎产出 witness 后，用两版策略再次真实求值并比对裁决与类别，不一致即 `EVIDENCE_MISMATCH`；测试再用**独立参考实现**复核（见下）。 |
| **不构建用户角色后台** | 无用户/角色/登录面。HTTP 仅有分析、见证、复核、审计接口；调用方仅用自报的不透明 `client_ref` 做关联。 |

### 失败类别（单列，不混入成功结果）
`PARSE_ERROR`、`SPACE_LIMIT_EXCEEDED`、`EVIDENCE_MISMATCH`、`BAD_SIGNATURE`、`NOT_FOUND`、`INVALID_REQUEST`。
解析器对无法**穷尽**分析的输入（内嵌 `*`、多个 `*`、未知算子、错误版本号、缺字段、类型错误）**显式拒绝并给定位**，而不是近似猜测。

---

## 2. 算法假设（重要）

1. **受限模式语言**：`Action`/`Resource`/`StringLike` 只支持“字面量”或“单后缀 `*` 前缀”（如 `photos/*`、`s3:Get*`）。这是可穷举的关键——成员关系只在前缀边界处变化。通用 glob/正则会使“集合是否扩大”不可判定，因此直接拒绝。
2. **边界代表完备性**：把两版所有前缀建成字符 trie；在每个节点发出“恰为该前缀”“沿某条边继续”“沿一条此处不存在的边继续”三类代表串。对任意字符串 `s`，都存在某个代表串 `r`，使 `s` 与 `r` 对所有模式的匹配向量完全相同（`tests/test_patterns.py` 对一个大规模暴力宇宙验证该性质）。
3. **请求空间 = 各轴笛卡尔积**：
   - principal：两版出现的每个身份，外加一个“不被任何语句命名”的合成身份；
   - action / resource：上述 trie 边界代表，外加合成无关值；
   - 每个被引用条件键一个域：string=边界代表+未知；number=各阈值/区间中点/端点外侧+未知；bool=true/false/未知；ip=各 CIDR 网络/广播地址及相邻外侧地址+未知；跨策略类型不一致则该域仅含未知。
   - 每个属性轴都含“未知值”，所以未知行为在每个请求形状上都被覆盖。
4. **有界**：笛卡尔积受 `space_cap`（默认 200000）约束；超限返回 `SPACE_LIMIT_EXCEEDED`，**绝不静默截断**。
5. 数字用 `Decimal` 精确比较；IP 用标准库 `ipaddress`。
6. **“确定扩大” vs “可能扩大”**：仅 ALLOW 才算“已证明可访问”；ALLOW 或 UNKNOWN 算“可能可访问”。`expands`（确定）与 `possibly_expands`（含 UNKNOWN）分开报告。

这些假设意味着：结论是对“该受限语言 + 该有界空间”的**穷尽**结论，而不是对任意真实流量的抽样估计。

---

## 3. 模块关系（各司其职，无硬编码演示）

```
osdiff/
  types.py     三值裁决 Verdict、迁移类别 Category、失败类别 Failure、Witness/RunResult
  patterns.py  受限模式解析 + trie 边界区域穷举（拒绝不可穷举语法）
  policy.py    规则/证据文档解析与校验（显式拒绝，带定位）
  kernel.py    安全内核：三值逻辑、Deny>Allow>默认拒绝、未知→UNKNOWN、逐步 trace
  universe.py  由两版策略边界构建有界请求空间（笛卡尔积 + cap）
  diff.py      差分编排：解析→建空间→穷举→归类→取见证→见证复核→签名
  evidence.py  请求身份(sha256 规范化)、未知值 JSON 标签、见证再判定
  signing.py   本地 Ed25519 密钥与签名（cryptography），载荷规范化
  store.py     SQLite 持久化，行级 run_id 状态隔离，签名公钥绑定
  audit.py     哈希链 + 逐条签名审计，关联 run_id/request_id/来源位置，可整链校验
  service.py   编排层：CLI 与 HTTP 共用，保证行为与审计一致
  api.py       FastAPI 审计/查询接口
  cli.py       命令行入口
  config.py    配置（参数 > 环境变量 > 配置文件 > 默认）
tests/
  oracle.py      ★ 独立参考实现：不 import 任何 osdiff，另一种写法（集合式 + Kleene）
  fixtures.py    本地合成策略夹具（重叠前缀/否定条件/无关规则/显式拒绝/IP/数值边界/坏文档）
  test_*.py      断言具体结果与失败类别；穷举交叉核验；见证在两版真实判定
```

### 独立测试与“答案不能由被测核心自己生成”
- `tests/oracle.py` **不依赖 osdiff 任何模块**，独立实现同一套语义。
- `test_kernel` 在 1200 个随机策略×请求上要求内核裁决 == oracle 裁决。
- `test_diff` 的 `test_oracle_agrees_at_every_bounded_space_point` 对 **10 个夹具对、穷举空间内每一个请求、新旧两版策略** 都要求内核与 oracle 一致；每个输出 witness 也由 oracle 独立重判。
- `test_patterns` 用独立暴力匹配器在 6 字符表 × 长度≤4 的宇宙上验证代表向量完备性。
- 失败类测试断言**具体类别**（PARSE_ERROR / SPACE_LIMIT_EXCEEDED）与细节，而非“接口能调用”。

### 可解释性
- 每条审计事件含 `run_id`（关联分析）、`request_id`（关联具体请求）、`stage`（关键步骤）、`caller_location`（产生于哪个文件:行）、策略版本指纹、`detail`。
- witness 的 `old_trace/new_trace` 展示每条语句的 scope/condition 判定与“为何 UNKNOWN”。
- API 把 `conclusion`（确定扩大/可能扩大/收缩）与 `uncertainty`（未知计数）**分字段**列出；失败用专用错误体 `failure_code/message/details`。
- 审计为哈希链（`prev_hash/entry_hash`）并逐条 Ed25519 签名；篡改任一行或 run 结果都会在 `/v1/audit/verify` 或签名校验中暴露。

---

## 4. 依赖版本

| 包 | 版本 | 用途 |
|---|---|---|
| Python | 3.10+（开发验证于 3.12.3） | |
| fastapi | 0.115.6 | HTTP 接口 |
| uvicorn[standard] | 0.32.1 | 本地服务 |
| pydantic | 2.9.2 | 请求模型 |
| cryptography | 43.0.3 | Ed25519 签名 |
| httpx | 0.27.2 | API 测试传输 |
| pytest | 8.3.4 | 测试框架 |

固定版本见 `requirements.txt`。

---

## 5. 本地验证命令与预期判断

```bash
# 1) 建虚拟环境并安装
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2) 跑全部独立测试（预期：全部通过）
python -m pytest -q
#   预期: 60 passed（含穷举交叉核验、1200 随机用例、CLI、API、篡改检测）

# 3) 命令行差分（退出码 0=无确定扩大；2=确定扩大；1=失败）
export OSDIFF_DATA_DIR=./.osdiff-data
python -m osdiff.cli diff examples/old.json examples/new.json
#   预期: expands proven: True；出现 EXPANSION_PROVEN 见证（bob、private 前缀等）；退出码 2

# 4) 未知条件值必须是 UNKNOWN
python -m osdiff.cli verify examples/new.json examples/request_unknown.json
#   预期: "verdict": "UNKNOWN"，reason 明确 “default is NOT to allow”，trace 标注 indeterminate

# 5) 审计链校验（哈希链 + 签名）
python -m osdiff.cli audit-verify
#   预期: {"ok": true, "events_verified": <N>}

# 6) 起 HTTP 接口（仅本地）
python -m osdiff.cli serve --host 127.0.0.1 --port 8080
#   POST /v1/diff            体: {"old_policy": {...}, "new_policy": {...}, "client_ref": "..."}
#   GET  /v1/runs/{id}         取完整结果
#   GET  /v1/runs/{id}/witnesses?category=EXPANSION_PROVEN
#   POST /v1/verify            单请求复核（可带 expected_verdict）
#   GET  /v1/audit?run_id=...  关联审计
#   POST /v1/audit/verify      整链完整性校验
```

**如何判断结果**
- `expands_proven_access=true` ⇒ 在受限空间内**确证**有请求从拒绝变允许，看 `witnesses` 里的具体请求。
- 仅 `possibly_expands=true` 而 proven 为假 ⇒ 差异由未知条件引起，必须补齐属性值后再判定，**不能当作已授权**。
- 请求失败（非 2xx / CLI 退出码 1）时读 `failure_code` 与 `details.errors` 定位；`SPACE_LIMIT_EXCEEDED` 会给出 `attempted` 与 `cap`。
- `audit/verify` 或 run 签名校验失败 ⇒ 落盘状态被篡改或密钥被换。

---

## 6. 测试状态（如实标记）

- **已运行并通过**：`python -m pytest -q` → 60 passed（Python 3.12.3，依赖版本如上）。
- 开发期使用的是隔离临时目录/内存数据；`./.osdiff-data` 与 `/tmp/osdiff-demo` 仅为手动演示产生，可随时删除。
- 未做：真实云策略/真实流量接入（按需求明确排除）；高并发压测；受限语言之外模式的支持（按设计直接拒绝）。
