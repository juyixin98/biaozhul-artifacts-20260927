# 列式文件统计审计后端（Parquet min/max · NULL 计数 · 排序统计）

审计 Parquet 文件 **页级 / 列块（行组）级** 的 `min`、`max`、`null_count`
以及页脚 `sorting_columns` 排序声明，独立重算真值并校验聚合关系；一旦
发现错误统计，就把该列标记为不可信，**查询层禁止再用该统计剪枝**，
强制扫描从而保证结果正确。

技术栈：Python 3.10+ · PyArrow（读数据）· FastAPI（验证接口）· SQLite（元数据事务）。
页级统计读取与"坏统计夹具"的改写由仓库内自带的、无第三方依赖的
Thrift Compact 编解码器完成（`src/colstats/parquet_codec.py`）。

---

## 模块划分（真实模块，非单文件脚本/桩）

| 层 | 文件 | 职责 |
|---|---|---|
| 独立配置 | `src/colstats/config.py`, `config.toml` | TOML + `COLSTATS_*` 环境变量覆盖，不依赖其它模块 |
| 格式适配 | `src/colstats/parquet_codec.py` | Thrift Compact 解析/序列化，页头/页脚字节级无损往返 |
| 格式适配 | `src/colstats/parquet_adapter.py` | 解析页/块统计与排序声明；读实际数据；按补丁重建文件（夹具用） |
| 逻辑序 | `src/colstats/ordering.py` | 按 Parquet total order 比较（NaN 分符号、±0.0 区分） |
| 执行内核 | `src/colstats/kernel.py` | 独立重算真值、逐项比对、页→块聚合校验、`can_prune` 决策、审计结论 |
| 元数据事务 | `src/colstats/store.py` | SQLite，审计头+全部发现在单事务内原子提交 |
| 验证接口 | `src/colstats/api.py` | FastAPI：审计/查询/纯剪枝决策/按 ID 检索 |
| 查询引擎 | `src/colstats/query.py` | 应用剪枝门禁执行谓词查询，对照"盲目信任统计"与"禁用后全扫" |
| 领域模型 | `src/colstats/models.py` | dataclass 模型 |
| 夹具 | `scripts/build_fixtures.py` | 生成 7 个文件夹（含正确/错误/无统计/全 NULL/混合 NaN/截断/坏排序） |
| 演示 | `scripts/demo.py`, `scripts/run_all.sh` | 本地端到端演示与一键复现 |

---

## 四条验收规则如何落地

### 规则 1：按逻辑类型比较，浮点 NaN 与有符号零单独定义

`ordering.py` 用 IEEE-754 total order 的整数映射，不使用 Python 自带的
`min`/`==`（它们把 NaN 当无序、把 `+0.0 == -0.0`）：

```
-Inf < … 负有限数 … < -0.0 < +0.0 < … 正有限数 … < +Inf < -NaN < +NaN
```

- `values_equal(-0.0, +0.0)` 为 `False`；
- `values_equal(NaN, -NaN)` 为 `False`，`values_equal(NaN, NaN)` 为 `True`；
- 真值重算时 **NaN 不参与 min/max**（Parquet 规范），但单独记录
  `contains_nan`；`-0.0`/`+0.0` 都在数据中时边界按 total order 取。

### 规则 2：字符串截断标志影响可信范围

Parquet Statistics 的 `is_min_value_exact` / `is_max_value_exact`
（字段 7/8）为 **false 表示该边界被截断**。规范保证的方向是：

- 截断下界 `trunc_min <= 真实最小值`；
- 截断上界 `trunc_max >= 真实最大值`。

审计校验该方向（违反即 `TRUNCATED_BOUND_OUTSIDE` 拒绝）。剪枝决策
（`kernel.can_prune`）据此收窄：

- 查询值落在边界的 **严格外侧** → 仍可 `PRUNE`；
- 查询值 **恰好等于被截断的边界** → `UNDECIDABLE`（必须扫）；
- 非字节列出现截断标志 → 直接拒绝。

### 规则 3：页级统计与行组（列块）统计的聚合关系须校验

对扁平列按数据页 `num_values` 顺序切分实际数据，逐页独立重算真值，并
校验：

- 每页 min/max/null_count 与该页真值一致；
- 各页 `null_count` 之和 == 列块 `null_count`
  （`PAGE_NULL_SUM_MISMATCH`）；
- 各页 `num_values` 之和 == 列块 `num_values`
  （`PAGE_VALUE_COUNT_MISMATCH`）；
- 各页 min/max 的 total-order 聚合 == 列块 min/max
  （精确边界要求相等；截断边界要求覆盖，`PAGE_AGGREGATION_MISMATCH`）。

### 规则 4：发现错误不得继续以该统计剪枝

审计结论落到每列的 `trusted` 布尔：列上出现任何 `ERROR` 即
`trusted[col] = False`。`can_prune(..., trusted=False)` 无条件返回
`UNDECIDABLE`，查询引擎把 `UNDECIDABLE` 与 `SCAN` 一样**实际扫描**。
因此坏统计绝不会导致漏行——`tests/test_pruning.py` 中对每个坏夹具都
断言"查询结果 == 独立暴力全扫"，且"盲目信任统计会给出错误 PRUNE"。

---

## 审计结论与错误语义（Verdict / Finding codes）

结论三态：

| Verdict | 含义 | 能否剪枝 |
|---|---|---|
| `ACCEPTED` | 全部列块/页统计与实际数据一致，聚合关系成立，排序声明有效 | 可信列正常剪枝 |
| `UNDECIDABLE` | 只有 WARNING（如统计缺失、不支持类型、嵌套列无法逐页校验） | 相关谓词不剪枝、全扫，但不算文件损坏 |
| `REJECTED` | 至少一条 ERROR | 出错列 `trusted=False`，永不据其剪枝 |

Finding `code`（失败类别，测试按具体 code 断言）：

| code | 级别 | 含义 |
|---|---|---|
| `MIN_MISMATCH` / `MAX_MISMATCH` | ERROR | 精确 min/max 与实际 total-order 极值不符 |
| `NULL_COUNT_MISMATCH` | ERROR | null_count 与实际不符 |
| `NULL_COUNT_MISSING` / `MIN_MAX_MISSING` | WARNING | 缺统计，无法据此剪枝 |
| `PAGE_NULL_SUM_MISMATCH` | ERROR | 各页 null_count 之和 != 列块值 |
| `PAGE_VALUE_COUNT_MISMATCH` | ERROR | 各页 num_values 之和 != 列块值 |
| `PAGE_AGGREGATION_MISMATCH` | ERROR | 列块 min/max 与各页聚合结果不一致/不覆盖 |
| `TRUNCATED_BOUND_OUTSIDE` | ERROR | 截断边界越过真实极值，外侧剪枝会漏行 |
| `SORTING_DECLARATION_VIOLATED` | ERROR | 页脚排序声明与实际数据顺序不符 |
| `UNSUPPORTED_TYPE` | WARNING | 物理类型不在审计支持范围（不剪枝） |
| `GOOD_STATS` | INFO | 全部通过的总结性记录 |

每个 Finding 都带：

- `locator`：`{row_group, column[, page][, bound]}`，可直接定位到页和字段；
- `request_id`：与 HTTP 请求头 `X-Request-ID` 关联；
- `expected` / `observed`：关键状态。**默认脱敏**（配置 `audit.expose_values=false`）：
  字节值只给长度 + SHA-256 前 12 位指纹，数值给类型与 `redacted` 标志，
  不打印任何真实业务数据；显式 `expose_values=true` 才输出明文。

剪枝决定三态：`PRUNE`（安全跳过）/ `SCAN`（相交必扫）/
`UNDECIDABLE`（缺失、截断边界或不可信，规则禁止剪枝）。

---

## 夹具（`tests/fixtures/`，全部本地合成）

| 文件夹 | 数据 | 文件内统计 | 期望结论 |
|---|---|---|---|
| `good_stats/` | 多页、含 NULL、负数、±0.0 | 正确 | ACCEPTED |
| `wrong_stats/` | 正确 | 块级/页级 min/max、null_count 被改错 | REJECTED |
| `no_stats/` | 正确 | 关闭统计写入 | UNDECIDABLE |
| `all_null/` | note/amount 全 NULL | 正确（无 min/max 合理） | ACCEPTED |
| `mixed_nan/data.parquet` | ±NaN、±0.0、±Inf、NULL | 正确 | ACCEPTED |
| `mixed_nan/bad_nan_stats.parquet` | 同上 | max 被改成 NaN | REJECTED |
| `truncated_strings/data.parquet` | alpha… 字符串 | 合法截断边界 `alph`/`b` | ACCEPTED |
| `truncated_strings/bad_truncated.parquet` | 同上 | 截断 max 越过真实上界 | REJECTED |
| `sorting_wrong/` | score 无序 | 页脚却声明升序 | REJECTED |

每个文件夹附 `expected.json`，记录数据真值、期望结论与失败类别。
夹具生成器只用适配层的**读写**能力；测试中的参考真值由
`tests/conftest.py` 用独立的纯 Python 实现计算（IEEE total-order 键、
集合计数），**不由被测内核自己生成**。

---

## HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `POST /audits` | body `{path, expose_values?}`，审计并落库，返回结论+findings |
| `GET /audits/{audit_id}` | 取审计详情（含定位与脱敏证据） |
| `GET /audits` | 最近审计列表 |
| `GET /audits/by-request/{request_id}` | 按请求标识检索 |
| `POST /query` | body `{path, column, predicate, value[, value_high]}`，返回每组剪枝决定、扫描行数、命中结果 |
| `POST /prune/check` | 不依赖文件，直接对给定统计+谓词返回 PRUNE/SCAN/UNDECIDABLE |

所有响应回带 `X-Request-ID`（可在请求头中自行指定）。

---

## 复现步骤

```bash
# 1) 安装依赖（建议虚拟环境）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 生成合成夹具
PYTHONPATH=src .venv/bin/python scripts/build_fixtures.py

# 3) 跑测试（断言具体结果与失败类别，实际执行并报告）
PYTHONPATH=src .venv/bin/python -m pytest tests/

# 4) 端到端演示
PYTHONPATH=src .venv/bin/python scripts/demo.py

# 或一键完成 2~4 + 实时健康检查
bash scripts/run_all.sh

# 5) 启动服务
PYTHONPATH=src .venv/bin/python -m uvicorn colstats.api:create_app --factory --port 8080
#   入口等价于：colstats-server（pip install -e . 后）
```

实时请求示例：

```bash
curl -s -X POST localhost:8080/audits -H 'Content-Type: application/json' \
  -H 'X-Request-ID: demo-1' \
  -d '{"path":"tests/fixtures/wrong_stats/data.parquet"}'

curl -s -X POST localhost:8080/query -H 'Content-Type: application/json' \
  -d '{"path":"tests/fixtures/wrong_stats/data.parquet","column":"id","predicate":"eq","value":500}'
# -> trusted=false，两个行组都 UNDECIDABLE 全扫，results=[500]（正确）
```

配置（`config.toml` 或环境变量）：

| 键 | 环境变量 | 默认 |
|---|---|---|
| `service.host/port` | `COLSTATS_SERVICE_HOST/PORT` | 127.0.0.1 / 8080 |
| `service.db_path` | `COLSTATS_SERVICE_DB_PATH` | data/audit.db |
| `audit.expose_values` | `COLSTATS_AUDIT_EXPOSE_VALUES` | false（脱敏） |
| `audit.max_pages_per_file` | `COLSTATS_AUDIT_MAX_PAGES_PER_FILE` | 100000 |
| `log.level` | `COLSTATS_LOG_LEVEL` | INFO |

---

## 设计说明与边界

- 页级真值切分按数据页 `num_values`（值槽位，含 NULL）顺序进行，适用于
  `max_repetition_level=0` 的扁平列；嵌套/重复列逐页校验降级为 WARNING，
  不影响列块级审计。
- 改写坏统计夹具时，页头一旦变化即丢弃其 CRC（无法重算），并同步
  `compressed/uncompressed_page_size`、列块偏移/大小、行组大小；数据
  载荷字节从不修改，所以"数据正确、只坏统计"得到保证（PyArrow 重读
  数据与原表 `equals`，由夹具构建与测试共同保证）。
- 元数据写入在单 SQLite 事务内提交；重复审计 ID 触发回滚时不会留下
  孤儿 findings（见 `test_transaction_atomicity`）。
