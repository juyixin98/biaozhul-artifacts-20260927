# colaudit — 列式文件 min/max、NULL 计数与排序统计审计后端

审计 Parquet 列式文件的列级 min/max、NULL 计数、NaN/有符号零与排序统计，
校验 **页 → 行组** 的聚合关系，并只允许**通过审计的受信统计**参与查询剪枝。
发现错误统计时禁用该统计、全表扫描兜底，保证查询结果仍然正确。

技术栈：Python 3.12 · FastAPI · PyArrow · SQLite（标准库 `sqlite3`）。
所有数据均为本地合成夹具，无外部账号与业务数据依赖。

---

## 1. 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 运行全部测试（必须实际执行）
python -m pytest

# 本地端到端演示（构建夹具 → 审计 → 剪枝查询 → 与独立 oracle 对照）
python demo.py

# 生成夹具到 fixtures/（测试会在临时目录自动生成，这一步供手工使用）
python tests/fixtures.py fixtures

# 启动 HTTP 服务
python -m colaudit           # 默认 127.0.0.1:8000，可配置 config.example.yaml
```

配置（优先级：环境变量 `COLAUDIT_*` > YAML（`COLAUDIT_CONFIG=path`）> 默认值）：

| 配置项 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `home` | `COLAUDIT_HOME` | `.colaudit` | 运行时目录，SQLite 库位于其下 |
| `fixtures_dir` | `COLAUDIT_FIXTURES_DIR` | `fixtures` | 数据集根目录 |
| `host` / `port` | `COLAUDIT_HOST` / `COLAUDIT_PORT` | `127.0.0.1:8000` | 服务监听 |
| `mask_sensitive` | `COLAUDIT_MASK_SENSITIVE` | `true` | 诊断中敏感列端点脱敏 |

---

## 2. 模块划分（真实模块，非单文件脚本/桩）

| 模块 | 职责 |
| --- | --- |
| `colaudit/config.py` | 独立配置（YAML + 环境变量） |
| `colaudit/logical.py` | 逻辑类型与比较语义（NaN、有符号零、布尔/字符串/日期） |
| `colaudit/stats.py` | 列统计结构、从数据重算、页→行组聚合、逐字段比对 |
| `colaudit/adapter.py` | 格式适配：Parquet 行组/逻辑页读取、footer 内嵌统计、`claims.json` 声明统计 |
| `colaudit/catalog.py` | SQLite 元数据事务（登记、审计运行、裁决、诊断事件） |
| `colaudit/audit.py` | **执行内核**：校验声明统计、聚合关系与内嵌来源，产出裁决与诊断 |
| `colaudit/prune.py` | 仅基于受信统计的页级剪枝判定 |
| `colaudit/query.py` | 剪枝查询执行 + 无统计全扫基线 |
| `colaudit/masking.py` | 敏感数据脱敏 |
| `colaudit/api.py` / `__main__.py` | FastAPI 验证接口与服务入口 |
| `tests/fixtures.py` | **独立**合成夹具构建器（只依赖 pyarrow/stdlib，不 import 被测核心） |
| `tests/oracle.py` | **独立** CSV 真值 oracle（只用 `csv` 与 stdlib） |

### 数据集目录布局

```
<dataset>/
  manifest.json     # 列逻辑类型/敏感标记、文件、行组、逻辑页切分
  claims.json       # 声明的页级与行组级统计（可缺失、可错误、可截断）
  data.parquet      # PyArrow 写入的真实数据（2 个行组，每行组 3 个逻辑页）
  ground_truth.csv  # 独立真值（测试 oracle 只读它，不读 Parquet/claims）
```

“逻辑页”是行组内按 manifest 声明的连续行切片：审计因此不依赖 Parquet
data page v1/v2 页统计的暴露程度，又能真实校验页→行组聚合；数据读取始终
来自 Parquet 本体。

---

## 3. 错误语义（verdict / failure 类别）

| 裁决 | 含义 | 决策 | 允许剪枝 |
| --- | --- | --- | --- |
| `ok` | 声明统计与重算事实逐字段一致，且页→行组聚合成立 | accept | ✅ |
| `stats_missing` | 无声明统计 | unknown（为何无法判定） | ❌ 全扫 |
| `embedded_inconclusive` | Parquet 内嵌统计缺失/不可表达 | unknown | ❌（重算事实一致时仍可 ok） |
| `count_mismatch` | 行数错误 | reject | ❌ |
| `null_count_mismatch` | NULL 计数错误 | reject | ❌ |
| `nan_mismatch` | NaN 计数错误 | reject | ❌ |
| `minmax_mismatch` | min/max 端点错误 | reject | ❌ |
| `signed_zero_mismatch` | +0.0/−0.0 标志错误 | reject | ❌ |
| `truncation_invalid` | 截断声明与真实端点不相容，破坏可信区间 | reject | ❌ |
| `truncation_mismatch` | 截断标志错误（如非字符串列挂截断标志） | reject | ❌ |
| `sorted_mismatch` | 有序性声明错误 | reject | ❌ |
| `aggregation_mismatch` | 行组声明 ≠ 其页级声明的聚合，或缺页导致聚合链断裂 | reject | ❌ |
| `embedded_conflict` | 声明统计与 Parquet footer 内嵌统计冲突 | reject | ❌ |

规则 4 的落地：**查询执行只读取 verdict=`ok` 的页级统计**；其他任何类别
（包括“无法判定”）都把该页标记为 `scanned_untrusted` 并整页扫描。
`/query` 响应同时返回无统计全扫基线与 `correctness.matches_baseline`。

### 比较语义（规则 1）

* **逻辑类型**：int / float / bool / string / date，不跨类型比较；日期按
  日历序数，布尔按 `false < true`，字符串按码点。
* **NaN**：排序时 NaN 大于一切非 NaN；SQL 式等值中 NaN≠NaN；六个比较谓词
  （`eq/ne/lt/le/gt/ge`）任一侧为 NaN 均不命中（三值逻辑 UNKNOWN）；
  NaN 是具体值，不计入 NULL，单列 `nan_count`。
* **有符号零**：作为“值” `−0.0 == +0.0`（排序序位相同）；作为 min/max
  **端点二者可区分**（min 端保留 −0.0、max 端保留 +0.0），并由
  `has_positive_zero` / `has_negative_zero` 单独记录。Parquet 内嵌统计
  不区分有符号零，交叉校验时不将其视为冲突。
* NULL 不参与 min/max 与有序性；`is_null` / `not_null` 与比较谓词语义分离。

### 截断标志与可信区间（规则 2）

* `min_truncated=true`：声明 min 是真实 min 的**前缀**，真实 min ≥ 声明
  min，**下界仍可用**（如 `lt` 剪枝不受影响）。
* `max_truncated=true`：前缀可能小于真实 max，**上界失效**，`gt/ge` 一律
  保守扫描；`eq` 目标落在声明上界右侧时也不剪枝。
* 声明值不是真实端点前缀、或落在真实区间错误一侧 → `truncation_invalid`
  （`truncated_invalid` 夹具即此情形）。

### 聚合关系（规则 3）

行组声明必须同时满足：

1. 与从 Parquet **重算**的行组事实逐字段一致；
2. 等于其全部页级声明的聚合（count/null_count/nan_count 相加，min/max
   取端点并传播截断标志，sorted 还要求页间端点衔接）；任一页缺声明则
   聚合链不成立 → `aggregation_mismatch`；
3. 与 Parquet footer 内嵌行组统计在其能表达的字段上不冲突。

---

## 4. 合成夹具（可定位到文件/行组/页/列）

| 数据集 | 数据 | 统计 | 预期 |
| --- | --- | --- | --- |
| `well_formed` | 正确 | 正确 | 32/32 全部 trusted |
| `bad_statistics` | 正确 | 注入 3 处错误（见下） | 精确命中错误位置 |
| `no_statistics` | 正确 | 无 `claims.json` | 全部 `stats_missing`，零剪枝 |
| `all_null` | 整列/整页全 NULL | 正确 | `ok`，端点为 None，全 NULL 页可安全跳过比较谓词 |
| `mixed_nan` | 普通浮点/NaN/+0.0/−0.0/NULL 混合 | 正确 | `ok`，NaN 与有符号零计数正确 |
| `truncated_string` | 正确 | **合法**截断 min（前缀） | `ok`，下界剪枝有效、上界剪枝被禁 |
| `truncated_invalid` | 正确 | **非法**截断（非前缀且越界） | `truncation_invalid`，禁用后查询仍正确 |
| `sensitive_demo` | 正确 | 正确，`name` 标记敏感 | 诊断端点显示 `[REDACTED]` |

`bad_statistics` 注入的错误可定位：

* `data.parquet / 行组0 / 页0 / score`：min 篡改为 −999（行组 min 同步污染）
  → `minmax_mismatch`；
* `data.parquet / 行组0 / 页1 / score`：null_count 虚增 5（行组未同步）
  → 页 `null_count_mismatch`，行组聚合链同时暴露不一致；
* `data.parquet / 行组1 / id`：真实乱序却声明 `desc` → `sorted_mismatch`。

测试不只检查“接口能调用”，而是断言具体命中行数/ID、页决策、失败类别与
脱敏输出；参考答案来自独立的 `ground_truth.csv` + `tests/oracle.py`，
**不由被测核心生成**。

---

## 5. HTTP 接口

| 方法/路径 | 说明 |
| --- | --- |
| `GET /health` | 健康检查，返回 request_id |
| `POST /datasets/register` | 登记数据集目录（校验 manifest 与 Parquet 一致） |
| `GET /datasets` | 已登记数据集 |
| `POST /audit` | 执行审计，事务落库，返回摘要 |
| `GET /audit/{run_id}` | 完整报告（裁决 + 诊断事件） |
| `POST /query` | 剪枝查询，附全扫基线与正确性判定 |
| `GET /datasets/{name}/diagnostics` | 最近审计的诊断事件 |

所有响应带 `X-Request-ID`；诊断事件携带 request_id、文件/行组/页/列定位、
`accept/reject/unknown` 决策、原因与关键状态。敏感列只输出计数/标志等
结构字段，min/max 输出 `[REDACTED]`（NULL 与 `NaN` 标记保留，因其不泄露内容）。

### curl 复现

```bash
python tests/fixtures.py fixtures
python -m colaudit &

curl -s -XPOST localhost:8000/datasets/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"bad","root":"fixtures/bad_statistics"}'
curl -s -XPOST localhost:8000/audit \
  -H 'Content-Type: application/json' \
  -d '{"dataset":"bad","mask_sensitive":false}'
curl -s -XPOST localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"dataset":"bad","column":"score","op":"gt","value":5.0}'
# 期望: 坏页 scanned_untrusted, correctness.matches_baseline=true
```

`op` 取值：`eq/ne/lt/le/gt/ge/is_null/not_null`；浮点 `value` 支持字符串
`"NaN"`；日期为 ISO `yyyy-mm-dd`。

---

## 6. 复现步骤汇总

```bash
source .venv/bin/activate
python -m pytest          # 50 个测试, 期望全部 passed
python demo.py            # 控制台查看 8 个夹具的审计/剪枝/脱敏全流程
```

测试分布：`test_logical.py`（比较语义）、`test_stats.py`（重算/聚合对照
独立 oracle）、`test_audit.py`（五类裁决与失败定位）、
`test_prune_query.py`（坏统计禁用后仍正确、截断可信区间、NaN/全 NULL）、
`test_catalog.py`（事务回滚）、`test_masking.py`（脱敏）、
`test_api.py`（端到端 HTTP）。
