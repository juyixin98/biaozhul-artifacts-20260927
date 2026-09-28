# Parquet 受限嵌套列表 / 可空结构 读写验证后端

一个多模块后端，用于验证 Parquet **受限（canonical 3-level）嵌套 LIST** 与
**可空 STRUCT** 的读写：内核自行解释 definition / repetition level，区分
空列表、NULL 列表与列表内 NULL，并保证**数据页边界不能截断父记录**；同时用
PyArrow（值级）与 fastparquet（页级 D/R）两个**相互独立**的实现做对照。

本仓库数据全部为本地合成夹具，不依赖外部服务或真实业务数据。

---

## 1. 技术栈与依赖版本

| 组件 | 版本 | 角色 |
| --- | --- | --- |
| Python | 3.12 | 运行时 |
| PyArrow | 17.0.0 | 值级对照 oracle（写真实 Parquet 并读回） |
| fastparquet | 2024.11.0 | 页级 D/R 对照 oracle（独立于被测内核解析页面） |
| FastAPI | 0.115.2 | 验证接口 |
| uvicorn[standard] | 0.30.6 | ASGI 服务 |
| pydantic | 2.9.2 | 请求/响应模型 |
| SQLite | 标准库 | 元数据/run 事件事务 |
| pytest | 8.3.3 | 独立测试 |

> fastparquet 仅作为**对照工具**（测试依赖级别）。被测内核不会用它生成预期
> 答案：手写预期树在 `fixtures/expected_trees.json`，值 oracle 来自
> PyArrow，页级 D/R oracle 来自 fastparquet 对 PyArrow 文件的独立解析。

---

## 2. 从干净目录复现

```bash
# 1) 创建虚拟环境并安装固定版本
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # 含 pytest / fastparquet

# 2) 运行全部测试（断言具体结果与失败类别，而非仅能调用接口）
python -m pytest -q

# 3) 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4) 另一个终端：发起示例请求
./scripts/run_example.sh

# 或：不启服务，直接生成跨页嵌套数据并验证
python scripts/generate_and_verify.py --records 2000 --page-bytes 128
```

关键配置（环境变量，带默认值）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `PV_DB_PATH` | `./data/verifier.db` | SQLite 元数据库 |
| `PV_PAGE_SLOT_TARGET` | `1000` | 内核分页软目标（叶子槽数） |
| `PV_PAGE_VERSION` | `2.0` | 对照 Parquet 文件数据页版本 |
| `PV_COMPRESSION` | `NONE` | 对照文件压缩 |
| `PV_STRICT_TYPES` | `true` | 严格拒绝不支持的逻辑类型 |
| `PV_LOG_LEVEL` | `INFO` | 结构化日志级别 |

---

## 3. 模块划分（实际承担职责，非演示桩）

```
app/
  config.py                 集中配置（依赖版本无关的可调参数）
  core/
    errors.py               错误码 + 严重度（FATAL/UNCERTAIN/INFO）+ 位置
    schema.py               DSL 模式 + 准入控制 + 每节点 D/R 元数据
    kernel.py               树值 <-> (D,R,value) 槽流 编码/解码（被测核心）
    levels_codec.py         RLE/bit-packed hybrid 层级编解码（v1 前缀）
    pager.py                记录边界安全分页 + 页不变量校验
    verifier.py             编排：预期树 / PyArrow 值 / fastparquet 页 三方对照
  adapters/
    pyarrow_adapter.py      DSL->PyArrow 类型、写真实 parquet、值读回
    level_oracle.py         fastparquet 独立解析每个数据页的 D/R 与边界
  metadata/
    store.py                SQLite run/event 事务存储
  api/
    models.py  service.py  routes.py    Pydantic / 事务服务 / FastAPI
  logging_config.py         关联 request-id 的结构化 JSON 日志
  tests/                    8 个测试文件，267 个测试
  fixtures/                 手写预期树 + 示例请求
scripts/                    复现脚本
```

---

## 4. 核心语义：定义层 / 重复层

内核自行解释（不是调用 PyArrow）：

* 每个 OPTIONAL 组贡献 1 个定义层；每个 REPEATED 组同时贡献 1 个定义层和
  1 个重复层。
* canonical 3-level LIST = 可选外层 `list` 组 + REPEATED `element` 组 + item。
* `list<int32>` 叶子 `max_def=3, max_rep=1`，真值表：

  | 数据 | D | R |
  | --- | --- | --- |
  | 值 `1` | 3 | 0（首元素）/1（后续元素） |
  | 列表内 NULL 元素 | 2 | 元素重复 R |
  | 空列表 `[]` | 1 | 0 |
  | NULL 列表 | 0 | 0 |

* `list<list<int32>>`（`max_def=5, max_rep=2`）完整真值表见
  `fixtures/expected_trees.json`，例如
  `[[1,2],[None]] / [] / None / [[],[3,None]]` 的
  D=`[5,5,4,1,0,3,5,4]`，R=`[0,2,1,0,0,0,1,2]`。
* 多层（最多 3 层）list 与连续 NULL 都有夹具和测试。

### 页边界不截断父记录

`core/pager.py` 只在 `R==0` 槽前切页；超过目标大小的单条记录放进一个带
`oversized=true` 标记的页，**绝不**跨页切开。`verify_pages` 会拒绝任何首槽
`R!=0` 的分页计划（测试 `test_verify_pages_rejects_mid_record_start` 断言
错误类别 `PAGE_TRUNCATES_RECORD` 与页号/列名位置）。对照写出的 PyArrow 文件
由 fastparquet 独立核验：**每个数据页首槽 R 必须为 0**。

### 列按同一记录边界对齐

各叶子列独立组装；`_assert_column_alignment` 保证每列 `R==0` 槽数都等于顶层
记录数，否则报 `COLUMN_RECORD_BOUNDARY_MISMATCH`。

### 不支持的逻辑类型明确拒绝

`map / decimal / date / timestamp / uuid / json / bson / enum / float(32) /
float16 / interval` 在 schema 准入阶段即返回 `UNSUPPORTED_LOGICAL_TYPE`
（`float32` 等另有专门原因说明）。legacy 2-level repeated 编码返回
`LEGACY_LIST_LAYOUT`；零字段 struct 返回 `EMPTY_STRUCT`（PyArrow 也无法写）。

---

## 5. 接口

| 方法 路径 | 用途 |
| --- | --- |
| `POST /api/v1/schema/admit` | schema 准入 + 每叶子 max D/R 预览 |
| `POST /api/v1/verify` | 完整验证（可用 `X-Request-ID` 关联身份） |
| `GET  /api/v1/runs/{id}` | 取持久化的 run 及其结构化事件 |
| `GET  /api/v1/runs` | 最近 runs |
| `GET  /healthz` | 存活探针 |

`POST /verify` 请求体见 `fixtures/request_example.json`。查询参数
`include=values,levels` 可回显解码值与每列 D/R。响应：

* `status`: `OK` / `UNCERTAIN` / `FAILED`
* `steps`: 每一步（schema_admission、kernel_encode/decode、expected_tree、
  kernel_paging、pyarrow_write_read/value_oracle、fastparquet_level_oracle）
* `findings`: 失败/不确定结论，**失败原因与不确定结论分开列出**，带
  `severity / code / message / location`（位置含 record / list_index /
  column / page / slot）
* `kernel_pages` / `oracle_page_count`: 内核分页与外部文件页数

### 已显式记录的跨实现方言（UNCERTAIN，不静默）

1. `DIALECT_NULL_STRUCT`：`list<struct>` 的 NULL 元素以及 standalone NULL
   struct 在 Parquet 物理层只有叶子 NULL 标记；PyArrow 读回时物化成“字段全
   NULL 的 struct”。物理 D/R 一致，仅组装出的对象身份不同，值级比较做了归一
   化并单列该结论。
2. `DIALECT_FASTPARQUET_NULL_STRUCT_D`：fastparquet 在独立（非 list）NULL
   struct 上对 struct 可选 D 层的读取会塌缩 ±1；R 流与 PyArrow 值 oracle 均
   确认内核，故仅作参考读取器差异上报。LIST 契约层级不在豁免范围。

---

## 6. 可解释性：请求身份 / 关键步骤 / 处理位置

* 每个请求带 `X-Request-ID`（未提供则生成），SQLite run 主键与所有日志事件
  都带该 id。
* 日志为 JSON（`app/logging_config.py`），含 `request_id / phase / status /
  detail`；失败时 detail 给出列、页号、槽位、记录索引。
* run 与事件在**单个 SQLite 事务**内落库（`metadata/store.py`），失败 run 也
  会以 `FAILED` 与其错误证据持久化。

---

## 7. 验收 / 测试命令

```bash
python -m pytest                 # 267 passed
python scripts/generate_and_verify.py --records 2000 --page-bytes 128
```

测试覆盖：schema 准入与拒绝类别、手写预期 D/R 真值表、空/NULL/列表内 NULL 三
分、3 层列表、连续 NULL、记录边界分页与损坏分页拒绝、层级 codec 与
fastparquet 双向互认、随机（多 seed）内核↔PyArrow 值一致、fastparquet 页级
D/R 与跨页 R=0 边界、错误值/类型/越界拒绝、HTTP API 与 SQLite 事务。

执行结果记录见 `RESULTS.md`。
