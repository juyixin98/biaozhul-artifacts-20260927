# 匿名化等价类风险检查后端

对**合成数据表**执行 **k-匿名（k-anonymity）** 与 **l-多样性（l-diversity）**
等价类风险检查，并给出**信息损失最优的泛化层级建议**的后端服务。

技术栈：Python 3.12 · FastAPI · SQLite · cryptography(Fernet) · Pydantic · pytest。
全部数据均为本地合成夹具，无生产账号、无真实业务数据、无外部服务依赖。

> ⚠️ **重要前提**：k-匿名与 l-多样性是针对特定重识别 / 属性披露攻击的
> **语法启发式指标**，**不构成完整隐私保证**。详见文末 [§限制](#6-限制与非目标)。

---

## 目录
1. [快速开始](#1-快速开始)
2. [工程结构与分层](#2-工程结构与分层)
3. [接口与示例调用](#3-接口与示例调用)
4. [核心算法](#4-核心算法)
5. [配置、密钥与状态隔离](#5-配置密钥与状态隔离)
6. [限制与非目标](#6-限制与非目标)
7. [测试与独立验证](#7-测试与独立验证)
8. [运维：审计链与日志](#8-运维审计链与日志)

---

## 1. 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock          # 安装锁定依赖

# 生成本地密钥（也可只用一次性密钥，见下）
.venv/bin/python -m app.cli gen-key

# 方式 A：本地一次性密钥（数据重启后不可解密，仅用于合成/演示）
ANON_ALLOW_EPHEMERAL_KEY=1 \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 方式 B：固定密钥（密文可跨重启解密），审计 HMAC 密钥自动从主密钥派生
ANON_ENCRYPTION_KEY="$(.venv/bin/python -m app.cli gen-key)" \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/health
# {"status":"ok","service":"anon-risk","version":"1.0.0","key_source":"ephemeral"}
```

端到端示例（成功 / k 不可达 / l 不可达 / 审计校验）：

```bash
bash examples/call_api.sh
```

交互式 OpenAPI 文档：<http://127.0.0.1:8000/docs>

跑测试：

```bash
.venv/bin/python -m pytest          # 60 个测试
```

---

## 2. 工程结构与分层

不是单文件、不是调用壳、没有固定返回值。按"解析/证据 → 安全内核 →
状态隔离 → 审计"分层：

```
app/
  config.py                 # 配置层：环境变量/.env，输入上限，密钥策略
  models.py                 # API 数据模型：显式角色声明，NULL 哨兵，输出模型
  service.py                # 编排层：解析→内核→密文存储→审计，生成 run_id/指纹
  main.py                   # FastAPI 入口、统一错误处理（异常≠成功）
  core/
    errors.py               # 失败码与类别（validation/hierarchy/threshold_unreachable/…）
    logging_setup.py        # 结构化 JSON 日志、StepLogger（进度+判定依据）
    parsing.py              # 规则/证据解析：角色、未知列、缺失=NULL、尺寸上限
    hierarchies.py          # 泛化层级：恒等层、覆盖、包含关系(单调性)强校验、信息损失
    anonymization.py        # 安全内核：真实等价类计数、k/l 判定、穷举+分支限界
  security/
    crypto.py               # Fernet 静态加密、输入指纹、密钥派生
    audit.py                # 只追加 JSONL + HMAC 哈希链，可离线验篡
  store/
    db.py                   # SQLite(WAL)：行数据仅密文落盘，运行结果不可变快照
  api/routes.py             # HTTP 路由
  cli.py                    # gen-key / verify-audit / fingerprint
tests/
  fixtures/*.json           # 合成小表 + 手工标注的期望结果（证据）
  reference_oracle.py       # ★独立参考预言机：不导入被测内核的朴素重实现
  test_*.py                 # 断言具体数值与失败类别的独立测试
examples/                   # 示例请求与调用脚本
```

**数据流**：原始 JSON → Pydantic 类型校验 → `parsing` 语义/规则校验
（规范化、NULL 保留）→ `anonymization` 在真实行上分组计数与搜索 →
脱敏结果（只有规模/计数）返回，原始行**仅以 Fernet 密文**写入 SQLite。

---

## 3. 接口与示例调用

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 版本与密钥来源 |
| POST | `/datasets` | 提交数据集（密文落盘），返回 schema_id/指纹/元数据 |
| GET | `/datasets` · `/datasets/{id}` | 列出/查看元数据（**不返回行数据**） |
| POST | `/analyze` | 提交即分析（先密文入库，再分析并持久化运行结果） |
| POST | `/datasets/{id}/analyze` | 对已存数据集用**新阈值**重放 |
| GET | `/runs/{id}` · `/runs` | 取回不可变运行结果 / 列出 |
| GET | `/audit` · `/audit/verify` | 审计记录 / 哈希链校验 |

### 3.1 请求体：列角色必须显式声明

```json
{
  "name": "example_patients",
  "k": 2, "l": 2,
  "columns": [
    {"name": "age",  "role": "quasi_identifier", "hierarchy": {"levels": [ ... ]}},
    {"name": "city", "role": "quasi_identifier", "hierarchy": {"levels": [ ... ]}},
    {"name": "diagnosis", "role": "sensitive"}
  ],
  "rows": [ {"age": 23, "city": "Haidian", "diagnosis": "flu"}, ... ]
}
```

* 每列角色显式声明为 `quasi_identifier` / `sensitive` / `insensitive`。
  至少一个 QI、一个敏感字段；QI 必须带泛化层级；否则返回具体错误码。
* 约束：`k >= 2`、`l >= 1`、`l <= k`，违反在模型层拒绝（422）。

### 3.2 泛化层级规则

`levels[0]` 必须是**恒等映射**（值→自身），覆盖样本中全部真实值；
后续每层把值映射到更粗标签。系统强校验**包含关系（单调性）**：

> 对任意值 x,y 与层级 h：若第 h 层 x,y 同组，则第 h+1 层也必须同组。
> 上层只能"合并"，不能"拆分"或交叉重排。

违规返回 `HIERARCHY_NOT_MONOTONE`，并指出层级与下层组（只给类别，不回显值）。
层级允许声明比当前样本更大的域（预定义字典），但信息损失按**样本内实际值**归一化。

### 3.3 NULL 不被悄悄移出样本

`null` / 空串 / 缺失键统一规范化为内部哨兵并**保留在样本中**：

* NULL 行照常参与等价类计数；
* NULL 在每个 QI 的每层**恒等传播、独立成组**，绝不与真实值合并；
* 响应含 `null_kept_in_sample=true` 与 `n_null_rows`，并给出说明性 warning。

### 3.4 成功响应（节选，不含任何真实取值）

```json
{
  "run_id": "run_…", "schema_id": "sch_…", "status": "succeeded",
  "k": 2, "l": 2,
  "chosen_levels": [{"column":"age","level":2},{"column":"city","level":2}],
  "info_loss": 0.6875,
  "info_loss_metric": "mean per-row group-expansion: … (group_size-1)/(domain_size-1) in [0,1]",
  "n_combinations_explored": 9,
  "summary": {"n_rows":6,"n_classes":2,"classes_below_k":0,"fraction_identifiable":0.0, …},
  "equivalence_classes": [
    {"class_index":0,"size":3,"distinct_sensitive":2,"max_sensitive_frequency":2,
     "meets_k":true,"meets_l":true,"risk_level":"low","contains_null_qi":false}
  ],
  "null_kept_in_sample": true, "n_null_rows": 0,
  "computation_trace": [ … ],
  "disclaimer": "k-anonymity and l-diversity … do NOT constitute a complete privacy guarantee …",
  "service_version": "1.0.0"
}
```

### 3.5 失败：异常/未知状态绝不统一返回成功

* **阈值不可达**：HTTP 200，但 `status` 为 `k_unreachable`/`l_unreachable`，
  `failure_code` 为 `K_UNREACHABLE`/`L_UNREACHABLE`，
  `failure_category="threshold_unreachable"`，并给出最粗层级下仍违规的
  真实等价类规模（不含取值）。
* **输入/规则错误**：HTTP 422，结构化 `error.code`/`error.category`，如
  `UNKNOWN_COLUMN`、`EMPTY_DATASET`、`HIERARCHY_NOT_MONOTONE`。
* **资源**：`RUN_NOT_FOUND`/`SCHEMA_NOT_FOUND` → 404。
* **未预期异常**：HTTP 500 `INTERNAL_ERROR`，审计记 `status=error`，不吞掉。

---

## 4. 核心算法

### 4.1 等价类与 k/l 判定（真实计数）

按所选层级向量把每行的 QI 元组泛化后分组，得到真实等价类。每个类计算：
`size`、不同敏感签名数 `distinct_sensitive`、最高频敏感值计数
`max_sensitive_frequency`、是否含 NULL。

* `size < k` → k 违规（`high` 风险）；
* `size >= k` 但 `distinct_sensitive < l` → l 违规（`medium`）；
* 否则 `low`。

优化器**只选择层级**，所有计数在每个层级向量上都从真实行重新分组得到，
不估算、不缓存、不可被信息损失目标改写。

### 4.2 信息损失

逐行逐 QI 的平均"组扩张"损失（generalization/precision 变体，∈[0,1]）：

```
loss(value v, 列 c, 层级 h) = (group_size(v,h) − 1) / (observed_domain(c) − 1)
总体 = 所有行 × 所有 QI 的平均值
```

NULL 贡献 0（缺失不是泛化造成的损失）；层级 0 恒为 0。

### 4.3 最优泛化搜索（穷举 + 安全剪枝）

搜索空间 = 各 QI 层级选择的笛卡尔积。

1. **先评估最粗层级（全部取顶）**。层级已验证单调，故若顶层都不满足
   k（或 l），则**任何**更细层级都不可能满足 → 立即返回
   `K_UNREACHABLE`/`L_UNREACHABLE`（可达性的数学判定，不是猜测）。
2. 顶层可行时以其为初始最优，DFS 枚举层级向量（QI 声明顺序、层级升序、
   后列变化最快），用**逐列可加损失下界**分支限界：某子树下界已不优于
   当前最优时整枝跳过。
3. 平局取枚举序最小向量 → 结果确定、可复现。
4. 每个评估/剪枝/最优点都写入 `computation_trace` 与结构化日志，
   含判定依据 `basis`。

剪枝只利用"逐列损失关于层级单调不减"这一可证性质，数学上不可能改变
最优解；测试用**无剪枝的独立预言机**对此做了交叉验证（见 §7）。

---

## 5. 配置、密钥与状态隔离

复制 `.env.example` 为 `.env` 或使用环境变量（统一 `ANON_` 前缀）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ANON_ENCRYPTION_KEY` | 空 | Fernet 主密钥；空则需允许一次性密钥 |
| `ANON_AUDIT_SIGNING_KEY` | 空 | 审计 HMAC 密钥；空则从主密钥派生 |
| `ANON_ALLOW_EPHEMERAL_KEY` | `0` | 允许进程内一次性密钥（仅本地/测试） |
| `ANON_DATABASE_PATH` | `data/anon.db` | SQLite 文件 |
| `ANON_AUDIT_LOG_PATH` | `logs/audit.jsonl` | 审计链文件 |
| `ANON_APP_LOG_PATH` | 空(stderr) | 结构化应用日志 |
| `ANON_MAX_ROWS/COLUMNS/QI/HIERARCHY_HEIGHT` | 1000/32/8/8 | 输入上限 |

**状态隔离**：每次操作用短连接（WAL + 写锁）；测试用独立临时目录实例化
整套组件。一次性密钥模式下，进程重启后旧密文不可解密（`DECRYPTION_FAILED`）、
旧审计链不可校验 —— 这是刻意的隔离语义。无密钥且未开启一次性模式时
**拒绝启动**，不会静默进入无保护状态。

---

## 6. 限制与非目标

k/l 指标只覆盖很窄的攻击面。本服务**明确不保证**以下内容（响应中附带
`disclaimer`）：

* **不防背景知识 / 组合攻击**：攻击者掌握外部数据时，k 匿名组仍可能被还原。
* **不防同质性/偏斜之外的推断**：l-多样性只数"不同敏感值个数"，不衡量
  熵/语义相近；高熵但语义同质（如两种相近诊断）仍会泄露。需要 t-closeness
  等更强模型（未实现）。
* **不防差分攻击 / 成员推断**：语法匿名化不提供差分隐私那种可证明的噪声界。
* **泛化字典由调用方负责正确性与完备性**；本服务只校验其结构/单调性，
  不判断业务语义（如年龄段是否合理）。
* 不做抑制（suppression）决策、不做数值型自动分箱、不做多维重编码；
  当前 `n_suppressed` 恒为 0。
* 规模上界内（默认 ≤1000 行、≤8 个 QI、层级高 ≤8）做笛卡尔积穷举；
  超大规模需引入 top-down 搜索/采样（未实现）。
* 加密保护的是**静态存储与传输边界**，不改变"分析结果本身可能泄露统计
  信息"这一事实；输出已最小化（只给规模/计数），但仍应按敏感中间产物对待。

---

## 7. 测试与独立验证

60 个测试，断言**具体结果与失败类别**，不是"接口能调用"。

* `test_hierarchies.py`：恒等层、覆盖、交叉/拆分违反单调性被拒、
  NULL 自动传播、深度上限。
* `test_parsing_null.py`：角色必须显式、NULL/缺失保留、未知列拒绝、
  空表/阈值非法拒绝。
* `test_exhaustive_optimum.py`：**小表对全部 9 个层级组合逐点比对**
  被测实现与独立预言机的类规模/不同敏感值/损失/可行性；多个 (k,l) 下
  最优层级与损失一致；50 行随机表（4×4×4=64 空间）验证剪枝不改最优；
  断言类规模之和恰为真实行数、成员下标覆盖全部行（真实计数不被篡改）。
* `test_l_diversity.py` / `test_unreachable.py`：敏感同质 →
  `L_UNREACHABLE`；最粗仍唯一 → `K_UNREACHABLE`；失败不返回成功指标。
* `test_null_behavior.py`：极小群体、单条 NULL 高危类、NULL 类计数。
* `test_api.py`：端到端状态码/错误体、**响应与审计中不出现任何真实
  城市/诊断值**、幂等重复提交、运行取回、404。
* `test_crypto.py`：密文落盘无明文、错钥 `DECRYPTION_FAILED`、
  一次性密钥跨进程不可解、无钥拒绝启动。
* `test_audit.py`：哈希链衔接、篡改/删记录可检出、不可达记 `failed`
  而非 `succeeded`、非法审计状态被拒。
* `test_logging.py`：日志带 `run_id`/`input_fingerprint`/`service_version`，
  含 `search_init/evaluate_top/verdict` 步骤与判定依据，失败运行记失败状态。

### 独立参考预言机（关键）

`tests/reference_oracle.py` **不 import 任何被测内核模块**，用最朴素的
查表、`dict` 分组、独立推导的损失公式与**无剪枝** `itertools.product`
全枚举重新实现一遍。测试同时对照两个**独立来源**：

1. 夹具 JSON 中**手工标注**的期望值（人算证据）；
2. 独立预言机的计算结果。

因此参考答案不是由被测核心自身生成的。

---

## 8. 运维：审计链与日志

审计为只追加 JSONL，每条含 `seq/ts/run_id/action/status/detail/prev_hash/record_hash`，
`record_hash = HMAC-SHA256(派生密钥, 序号|时间|run|动作|状态|prev_hash|规范化正文)`。
状态仅允许 `succeeded|failed|error`：业务失败记 `failed`，未预期异常记
`error`，绝不把失败记成成功。

```bash
# HTTP 校验
curl -s http://127.0.0.1:8000/audit/verify

# 离线校验（固定密钥模式）
ANON_ENCRYPTION_KEY="$K" .venv/bin/python -m app.cli verify-audit --path logs/audit.jsonl
```

篡改任意字段或删除一行都会导致 `prev_hash` 断链或 HMAC 不匹配。
应用日志为单行 JSON（stderr 或 `ANON_APP_LOG_PATH`），可用 `run_id`
或输入 `input_fingerprint`（SHA-256 截断，不可逆）关联一次运行的全部步骤。
