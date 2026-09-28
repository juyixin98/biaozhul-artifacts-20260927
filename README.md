# Parquet 受限嵌套列表 / 可空结构读写验证后端

一套**多模块 Python 后端**，验证 Parquet 在受限逻辑类型表面下，对
**受限嵌套列表（多层 list、空列表、NULL 列表、列表内 NULL）与可空 struct**
的写入 / 读取正确性。

核心机制——**definition level / repetition level（DL/RL）的编解码、Dremel 树
重建、记录对齐的页切分、RLE/bit-packed hybrid、PLAIN 值编码、Thrift compact、
Parquet v1 文件读写——全部自行实现**；[PyArrow](https://arrow.apache.org/docs/python/)
仅作为**独立对照实现（oracle）**，不参与被测内核的任何预期答案生成。

---

## 1. 它实际做什么

给定一份受限 schema 与一批记录（JSON 树），后端执行一条可解释的流水线：

1. **模式适配（format/kernel）**：解析受限 schema；不支持的逻辑类型在这一步被
   明确拒绝（返回命名错误类别，绝不静默重解释）。
2. **执行内核（kernel）**：自研 DL/RL 算法把记录树编码成每个叶子列的
   `(definition_level, repetition_level, value)` 三元组事件流。
3. **页切分（kernel/pages）**：以**完整记录**为最小单位规划数据页，页边界
   **绝不会截断一条父记录**；可用 `force_page_after_records` 强制跨页以测试
   重组。
4. **格式适配（format）**：自研 Thrift compact 编解码 + 标准 Parquet v1
   `PAR1` 文件（data page v1、PLAIN 值、RLE/bit-packed hybrid level、
   UNCOMPRESSED），写出真实 Parquet 字节，PyArrow 可直接读取。
5. **自读回（format）**：用自研读取器解析页、重组 DL/RL、按记录边界对齐列、
   Dremel 重建树，错误定位到**页号 + 页内位置 + 列路径**。
6. **PyArrow 独立对照（oracle）**：
   - PyArrow 独立写 / 读同一批记录，树与我们对比；
   - **PyArrow 读我们写的文件**、**我们读 PyArrow 写的文件**（双向字节互通）；
   - 另有 fuzz 测试直接解码 PyArrow 文件里的**物理 RL/DL 字节**与我们内核
     逐条比对。
7. **元数据事务（storage）**：SQLite 原子记录请求、逐步步骤、制品路径、失败
   类别与“不确定结论”。

### 三种列表状态的区分（核心契约）

以可选 `list<list<int32>>` 为例（叶子最大 DL=5，最大 RL=2）：

| 记录状态 | 叶子事件 (DL, RL, value) |
|---|---|
| NULL 外层列表 | `(0,0,null)` |
| 空外层列表 `[]` | `(1,0,null)` |
| 列表内 NULL 元素 | 元素 DL 停在“元素缺失”那一级 |
| `[[60,70]]` | `(5,0,60),(5,2,70)` |
| `[[40],[50]]` | `(5,0,40),(5,1,50)` |

固定重复层深度让 `[[1,2]]`（RL `0,2`）与 `[[1],[2]]`（RL `0,1`）在字节上
可区分——这是本项目调试中通过解码 PyArrow 权威字节确认的关键规则。

### 明确支持 / 拒绝的类型

- **支持**：`boolean int32 int64 float double string`、`struct`（含零字段空
  struct，见下）、标准 3 层 `LIST`（可多层嵌套，元素可为 struct/list/基元）。
- **明确拒绝**：`decimal date timestamp time interval uuid json bson
  float16 enum map int8/16/uint* null(逻辑类型)`、2 层遗留 list、字典/DELTA
  编码、v2 数据页、压缩（SNAPPY 等）。拒绝时返回
  `UNSUPPORTED_LOGICAL_TYPE` 等具名类别。

### 已知物理限制（作为“不确定结论”单列）

- **零字段 struct**（`struct<>`）在 Parquet 中没有任何叶子列，无法仅从文件
  字节恢复其 per-record 存在性；PyArrow 甚至拒绝写它
  （`Cannot write struct type with no child field to Parquet`）。后端：
  - 仍用内核对其做树级往返验证（存在性带外携带）；
  - 把“字节级 PyArrow 对照对该字段不可得”列入响应的 **`uncertainties`**，
    与硬失败 / 警告严格分开。

---

## 2. 模块结构（职责分离，非硬编码演示）

```
app/
  config.py                 # 环境变量配置（本地 SQLite / 制品目录 / 页大小）
  logging_utils.py          # 结构化 JSON 日志，每行带 request_id
  api_models.py             # 请求 / 响应 Pydantic 模型
  service.py                # 验证编排：失败类别、步骤、不确定性、对照
  main.py                   # FastAPI 路由
  kernel/
    schema.py               # 受限类型系统 + 不支持类型显式拒绝 + 叶子 max DL/RL
    levels.py               # ★ 自研 DL/RL 编码 + Dremel 树重建（核心机制）
    pages.py                # 记录对齐的页规划（页边界不截断父记录）
  format/
    thrift.py               # ★ 自研 Thrift compact 协议编解码
    parquet_thrift.py       # parquet.thrift 元数据结构定义（精简子集）
    encodings.py            # ★ RLE/bit-packed hybrid（读全支持，写用 RLE）
    plain.py                # PLAIN 值编码（bool/int/float/double/string）
    parquet_io.py           # ★ 自研 Parquet v1 写 / 读器
  oracle/
    __init__.py             # PyArrow 独立桥（自己建数组、自己写读、返回纯树）
  storage/
    metadata.py             # SQLite 元数据事务（requests/steps/artifacts）
tests/
  fixtures.py               # 手写预期树 + 逐记录 (DL,RL,value) 预期
  test_schema.py            # 类型拒绝 / max level 断言
  test_levels_kernel.py     # 精确 DL/RL 断言、三态区分、记录边界失配
  test_encodings.py         # hybrid / PLAIN / thrift 的字节级断言
  test_parquet_io.py        # 双向互通、跨页重组、页号、不可读类型拒绝
  test_fuzz_oracle.py       # 随机嵌套数据 vs PyArrow 物理 RL/DL 字节
  test_service.py           # 失败类别 / 步骤 / 不确定结论 / 持久化
  test_api.py               # HTTP 层（request_id 关联、404 等）
examples/                   # 请求样例
```

---

## 3. 从干净目录复现

要求：**Python 3.11+**（验收环境为 3.12.3），仅需本地文件系统，无外部账号。

```bash
# 1) 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2) 安装固定版本依赖
python -m pip install --upgrade pip
pip install -r requirements.txt

# 3) 运行全部测试（独立断言，非“接口能调用”级别）
python -m pytest tests/ -v

# 4) 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### 请求样例

```bash
# 正常的多层嵌套（含跨页强制切分）
curl -s -X POST http://127.0.0.1:8000/api/v1/validate \
  -H 'Content-Type: application/json' \
  --data @examples/validate_nested.json | python -m json.tool

# 不支持的逻辑类型被明确拒绝
curl -s -X POST http://127.0.0.1:8000/api/v1/validate \
  -H 'Content-Type: application/json' \
  --data @examples/validate_unsupported.json | python -m json.tool

# 健康检查 / 按请求身份查询 / 列表
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/api/v1/requests/demo-nested-001
curl -s http://127.0.0.1:8000/api/v1/requests
```

### 关键环境变量（均有本地默认值）

| 变量 | 默认 | 含义 |
|---|---|---|
| `PNV_DB_PATH` | `data/validation.db` | SQLite 元数据库 |
| `PNV_ARTIFACT_DIR` | `data/artifacts` | 写出的 `.parquet` 制品目录 |
| `PNV_DEFAULT_PAGE_SIZE` | `1024` | 数据页目标字节（不会切记录） |
| `PNV_LOG_LEVEL` | `INFO` | 日志级别 |
| `PNV_LOG_JSON` | `true` | JSON 结构化日志（含 request_id） |
| `PNV_MAX_RECORDS` | `20000` | 单请求记录上限 |

---

## 4. 接口结果与日志如何“可解释”

响应（以及 SQLite 中的历史记录）包含：

- `request_id`：关联请求身份（客户端可指定，否则生成 `req-…`）；
- `steps[]`：每个关键步骤（`schema_parsed` → `levels_encoded` →
  `self_roundtrip`（含**逐页页号/记录布局**）→ `expected_tree_asserted` →
  `oracle_roundtrip` → `cross_interop` → `validation_passed`），各自带状态与
  细节；
- `mismatches[]`：失败时给出**失败类别**、记录号、列路径、**页号 / 页内位置**、
  期望值与实际值；
- `uncertainties[]`：无法确证的结论（当前为零字段 struct 的字节不可见性），
  与硬失败分开；
- `warnings[]`：非致命问题（如某次对照无法运行）；
- `artifact`：自研文件与 PyArrow 对照文件的路径 / 字节数，可离线复查。

日志为每行一条 JSON，恒定携带 `request_id`、级别、步骤、版本/位置等字段，便于
按请求聚合排查。

失败类别枚举：`UNSUPPORTED_LOGICAL_TYPE`、`SCHEMA_INVALID`、`INVALID_RECORD`、
`ROUNDTRIP_MISMATCH`、`EXPECTED_TREE_MISMATCH`、`ORACLE_MISMATCH`、
`PAGE_BOUNDARY_VIOLATION`、`PARSE_ERROR`、`INTERNAL_ERROR`。

---

## 5. 设计要点与取舍

- **为什么自己写 Parquet 而不是只算 level**：任务要求核心机制不能由硬编码演示
  替代，故连 Thrift compact、hybrid level、PLAIN、footer、data page v1 都
  自行实现；这也让“PyArrow 读我们的字节 / 我们读 PyArrow 的字节”成为真正的
  字节级互通验证，而非只在内存里比对。
- **写用 RLE、读支持 RLE + bit-packed**：RLE 对任意 run 长度（含 1）都合法，
  可保证页内任何位置都不出现“补零 run”，从根本上避免页边界 / 父记录被伪值
  污染；bit-packed 解码路径完整实现并用 PyArrow 产生的页覆盖测试。
- **预期答案不来自被测内核**：`tests/fixtures.py` 手写记录树和逐记录
  `(DL,RL,value)`；PyArrow 作为第二独立实现；fuzz 测试用随机数据比对物理字节。
