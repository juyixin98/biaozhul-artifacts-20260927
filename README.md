# 两级裁剪后端（Directory Partition + File Statistics Pruning）

一套多模块后端，对"**目录分区 + 文件统计**"两级数据做**保守裁剪（conservative pruning）**：
只有在给出"该分区/文件**不可能**匹配任何行"的确定性证据时才裁剪；统计缺失、可能截断、
NULL 计数未知等一切不确定情形一律**保留文件**，保证**零漏行**。

- 语言/栈：Python 3.12 · FastAPI · PyArrow（真实 Parquet 读写与统计）· SQLite（元数据事务）
- 固定时区与日期变换版本：UTC、`dateconv-1.0.0`（原生支持负 epoch）

---

## 1. 核心安全原则

### 1.1 分区值是"原列的变换"，不能拿桶编号当原值比较
月分区桶值 `event_ts=2024-02` 是 `UTC epoch 秒 → civil 月份` 的**变换结果**，不是原值。
因此内核**从不**把字符串桶值与谓词的原时间戳直接比较，而是从谓词的原值区间
**反推候选月桶集合**（`candidate_month_buckets`）：

- 日期范围 `2024-02-01..2024-02-29` → 候选桶 `{2024-02}`，可安全裁掉其它整月；
- 开区间上界恰为某月首时刻（`[..., 2024-03-01 00:00 UTC)`）时，3 月桶可安全排除；
- 非月首的日级边界，相邻桶整月保留（桶内仍可能有满足值）——保守；
- `IS NULL / NOT NULL` **不能**在分区层判定：NULL 行不产生任何桶值，其物理位置
  无法由目录推出，交给第二层的 `null_count`。

每个被裁剪目标都带 **原因码 + 人类可读理由 + 证据**（见 `PruneReason`）。

### 1.2 统计缺失或可能截断时保留文件
- `present=False`（统计缺失）→ 该列任何数据谓词都不裁剪；
- `truncated=True`（字符串 min/max 仅保留前缀，真实极值未知）→ 不裁剪；
- `null_count=None` → NULL 谓词不裁剪。

### 1.3 时区与日期版本固定
`src/pruning/versions.py` 冻结 `dateconv / stats / metadata schema / kernel` 版本，
时间戳一律按 UTC，civil-date 用纯整数 Howard Hinnant 互逆算法，**负时间戳结果确定**，
不读取系统本地时区。所有计划、审计与日志都回带这些版本号。

---

## 2. 模块结构

```
src/pruning/
  versions.py      版本固定（时区/日期/统计/schema/内核）
  transforms.py    UTC epoch<->civil 日期、月桶反推（负时间戳安全）
  model.py         谓词 / 列统计 / 文件 / 分区 / 带原因码的决策
  kernel.py        执行内核：分区层 + 文件统计层保守裁剪
  adapter.py       格式适配：PyArrow 写/读 Parquet，提取 min/max/null_count
  catalog.py       元数据事务：SQLite，单 BEGIN IMMEDIATE 事务 + 审计表
  validation.py    独立参考真值：PyArrow Compute 逐行全扫描，零漏行比对
  service.py       编排（API 边界 <-> 核心，核心框架无关）
  api.py           FastAPI 验证接口
  logging_config.py 结构化 JSON 日志（request_id/step/version/location）
  config.py / schemas.py
tools/make_fixtures.py  合成夹具生成器（月分区、负时间戳、NULL、长字符串）
tests/             独立测试（变换/内核/端到端全扫描/随机差分/HTTP）
```

"参考答案不全部由被测核心自身生成"：`validation.py` 用 **PyArrow Compute 独立逐行扫描**
每个物理 Parquet 文件构造布尔 mask，不调用内核任何裁剪函数；随机差分测试另用独立随机
数据与谓词生成器。

---

## 3. 安装（锁定依赖）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock        # 或 pip install -e .[test]
```

锁定版本：pyarrow 25.0.1 · fastapi 0.115.6 · pydantic 2.10.4 · uvicorn 0.34.0
· httpx 0.28.1 · pytest 8.3.4。

## 4. 快速开始

```bash
# 生成合成 Parquet 数据集（data/events/event_ts=YYYY-MM/part-*.parquet）
PYTHONPATH=src python -m tools.make_fixtures --root data

# 启动服务
PYTHONPATH=src PRUNING_DATA_ROOT=data PRUNING_DB=data/catalog.sqlite \
  .venv/bin/python -m uvicorn pruning.api:app --host 127.0.0.1 --port 8000
```

### 示例调用

```bash
# 注册（扫描目录、读真实 Parquet 统计、事务化写 SQLite）
curl -s -X POST localhost:8000/register -H 'Content-Type: application/json' -d '{
  "table":"events",
  "truncated_string_columns":["region"],
  "truncate_prefix_len":4}'

# 两级裁剪 + 独立全扫描校验（月分区 + 日范围）
curl -s -X POST localhost:8000/validate -H 'Content-Type: application/json' -d '{
  "table":"events","request_id":"demo-1","predicates":[
    {"column":"event_ts","kind":"range",
     "lower":"2024-02-01","upper":"2024-02-29","upper_inclusive":true}]}'

# 负时间戳（1969-12）
curl -s -X POST localhost:8000/validate -H 'Content-Type: application/json' -d '{
  "table":"events","predicates":[
    {"column":"event_ts","kind":"range",
     "lower":"1969-12-01","upper":"1969-12-31","upper_inclusive":true}]}'

# NULL 谓词（走文件 stats 层，不在分区层裁）
curl -s -X POST localhost:8000/validate -H 'Content-Type: application/json' -d '{
  "table":"events","predicates":[{"column":"event_ts","kind":"is_null"}]}'

# 审计：按 request_id 取回每个目标的裁剪原因码/证据/层级
curl -s localhost:8000/requests/demo-1
```

### 谓词格式
`{column, kind, ...}`，`kind ∈ range | eq | in | is_null | not_null`：
- `range`：`lower/upper`（epoch 秒数值，或 `YYYY-MM-DD` 日期串），
  `lower_inclusive`（默认 true）、`upper_inclusive`（默认 false）；
  日期上界按**整日**解释（`<= 2024-02-29` 即 `< 2024-03-01 00:00 UTC`）。
- `eq`：`value`；`in`：`values`；多个谓词之间为 **AND**。

---

## 5. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/register` | 扫描目录、读 Parquet 统计、事务化注册 |
| POST | `/plan` | 仅两级裁剪，返回带原因码的决策与分层裁剪量 |
| POST | `/validate` | 裁剪 + PyArrow 全扫描独立比对，返回零漏行结论 |
| GET  | `/tables` / `/tables/{name}` | 已注册表 / 表元数据 |
| GET  | `/requests/{request_id}` | 裁剪审计（决策、证据、版本） |
| GET  | `/healthz` / `/versions` | 健康 / 版本集合 |

`/validate` 响应关键字段：
- `status`（pass/fail）、`zero_missed_matches`（最关键的零漏行标志）；
- `kernel_selected_files`（内核将扫描）与 `truly_matching_files`（全扫描真值）；
- `layer_pruning`：`files_pruned_by_partition` / `files_pruned_by_stats` / `files_selected`；
- `failures`：分类为 `missed_match / missing_file / type_mismatch / unknown_column /
  selected_no_match`（最后一类是**合法保守冗余**，非错误，单列展示）。

日志为每行 JSON，含 `request_id / step / location / versions / reason /
uncertain / failure_category`，失败与不确定结论分别用对应字段单列。

---

## 6. 测试（真实执行结果）

```bash
. .venv/bin/activate && python -m pytest -q
# 43 passed
```

覆盖：
- 月分区 + 日范围、开区间月首；
- 负 epoch（1969-12）与 civil-date round-trip / 标准库交叉核对；
- `IS NULL / NOT NULL`（分区层不裁、stats 层按 null_count 精确裁）；
- 截断字符串统计强制保留；缺失统计、未知 null_count 强制保留；
- **随机差分**（5 种子 × 60 谓词 = 300 例，断言选中集 ⊇ 真实匹配集）；
- HTTP 接口、404、未知列失败类别、审计可追溯。

测试断言**具体文件集合、匹配行数、分层裁剪量与失败类别**，不止"接口能调用"。

## 7. 剩余限制（如实说明）
1. min/max 是**极值**统计：若谓词点落在 `[min,max]` 区间内但实际不存在（如
   `region='eu-west'` 而区间为 `[ap-south, us-east]`），文件只能保守保留——这是
   min/max 统计的固有粒度，需要 bloom filter / 字典等更强统计才能消除。
2. 字符串裁剪依赖读取器能拿到完整极值；本项目用"前缀截断"模拟不可靠读取器。
3. IN 当前按候选值**跨度**判不相交（充分非必要），跨度过松时保守保留。
4. 仅实现单表月分区（`month`）一种变换；多列分区、小时/日桶与 OR 嵌套谓词未覆盖。
5. 全扫描 oracle 是正确性基准，会读全部数据，仅供验证/诊断，不在热路径使用。
