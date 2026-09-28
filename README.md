# Morton (Z-order) 聚簇列式范围查询后端

多维**整数列**的 Morton（Z-order/位交错）编码聚簇与多维范围查询后端。
产物是本地列式分块（PyArrow IPC）+ SQLite 元数据事务 + FastAPI 验证接口。
全部数据来自确定性的本地合成夹具，无外部账号、无真实业务数据。

---

## 1. 架构与模块职责

| 模块 | 职责 |
|---|---|
| `zindex/encoding.py` | **格式适配/编码内核**：每维固定位宽与有符号 zig-zag 映射；MSB 优先的位交错 Morton 码（Python 大整数，≤128 位，高位不截断）；查询盒→保守一维码区间的前缀胞腔分解（带预算，精确/保守标注） |
| `zindex/chunkstore.py` | **本地列式分块格式**：`__row_id`(u64)、`__mc_hi`/`__mc_lo`(u64,u64)、原始整数列(i64)；按码排序；Arrow IPC 原子写；`.meta` 旁车 |
| `zindex/catalog.py` | **元数据事务**：SQLite（WAL），schema、单调计数器、chunk 清单、请求审计日志；所有多写在 `BEGIN IMMEDIATE` 事务中 |
| `zindex/kernel.py` | **执行内核**：原始盒→无符号边→区间分解→chunk 剪枝→128 位码区间下推（扁平 OR mask）→numpy 在**原始整数列**上做精确残余过滤→稳定 row id 返回 |
| `zindex/ingest.py` | 入库、schema 管理、**重写/压实**（按码全局重排，`__row_id` 逐行保留，chunk 元数据原子换入后才删旧文件） |
| `zindex/fixtures.py` | 确定性合成数据（`uniform` / `corners` / `sparse_hd` / `grid`），独立 LCG |
| `zindex/api.py` | **验证接口**（FastAPI）：统一响应信封、请求身份、分步解释、错误与不确定单列、请求审计 |
| `zindex/config.py`, `zindex/logging_setup.py` | 配置（JSON + `ZINDEX_*` 环境变量覆盖）、JSON 结构化日志（带 `request_id`） |
| `tests/` | **独立测试**（pytest），参考实现为朴素逐维比较/独立位反交错，不引用被测内核生成答案 |
| `scripts/validate.py` | **保留的验证过程**：边界坐标/负数/薄盒/高维稀疏 vs 全扫描，输出 `results/validation.json` |

### 核心机制（均为真实实现，无硬编码演示）

- **固定位宽 + 有符号映射**：schema 一旦写入不可变；有符号维用 zig-zag
  `u=(v<<1)^(v>>(b-1))`（0,-1,1,-2,2… 单调），无符号维恒等映射。
- **交错不截断高位**：每层从所有“仍存活”维的最高位开始交错，短维自然停止；
  码为 Python `int`，总位宽上限 128，落盘为 `(hi64,lo64)`。
- **盒分解**：以“前缀胞腔”覆盖无符号盒，胞腔码构成单个连续整数区间；
  完全在内→精确区间，部分相交→按**体积浪费最大优先**继续分裂，
  不相交→丢弃。预算（区间槽位）不足时，把剩余部分以**保守整胞腔**发出——
  只可能扩大候选，绝不可能漏掉命中；`budget_exhausted` 与保守区间数如实上报。
- **精确残余过滤**：所有命中最终都在**原始 i64 整数列**上逐维比较确认，
  保守区间带来的额外候选在此被剔除，保证语义精确（零漏行、零假行）。
- **稳定行身份**：row id 由计数器在建块前预留，永不复用；重写只重排数据，
  row id 随行走；新 chunk 用新 id，旧文件在目录事务提交后才删除。

> 实现注记：区间下推**没有**用一棵深层嵌套的 `|` Arrow 表达式交给
> `Table.filter`。Arrow 18 的表达式 Canonicalize 会对深层 OR 递归并**原生栈溢出
> (SIGSEGV)**（已用 gdb 定位到 `ModifyExpression`）。内核改为对每个区间用扁平的
> `pc.equal/greater/...` 直接算布尔掩码并用 `pc.or_` 归并，语义等价且稳定。

---

## 2. 环境与依赖版本

- Python **3.12.3**（已在 Linux 6.8 / Ubuntu 验证）
- 关键依赖（精确版本见 `requirements.txt`）：
  `pyarrow==18.1.0`、`fastapi==0.115.6`、`uvicorn[standard]==0.34.0`、
  `pydantic==2.10.4`、`numpy==2.2.1`、`httpx==0.28.1`、`pytest==8.3.4`

从干净目录复现：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
# 或： make install
```

---

## 3. 配置

`configs/dev.json`（默认）：

```json
{
  "data_dir": "data",
  "catalog_path": "data/catalog.sqlite",
  "log_file": "data/zindex.log",
  "log_level": "INFO",
  "default_chunk_capacity": 8192,
  "default_max_intervals": 4096
}
```

可用环境变量覆盖（测试即用此隔离）：`ZINDEX_DATA_DIR`、`ZINDEX_CATALOG_PATH`、
`ZINDEX_LOG_FILE`、`ZINDEX_LOG_LEVEL`、`ZINDEX_CHUNK_CAPACITY`、
`ZINDEX_MAX_INTERVALS`。

---

## 4. 运行

### 4.1 独立测试（断言具体结果与失败类别）

```bash
.venv/bin/python -m pytest -q          # make test
```

### 4.2 保留的验证过程（边界/负数/薄盒/高维 vs 全扫描）

```bash
.venv/bin/python scripts/validate.py   # make validate
# 机器可读： results/validation.json
```

脚本对每个场景统计：编码解码往返（含极值/负数）、零漏行零假行（对照独立朴素
全扫描）、各预算下的候选膨胀比、选中/跳过 chunk 数与物理读取字节数、重写后
row id 稳定性。

### 4.3 启动 HTTP 服务

```bash
.venv/bin/python -m uvicorn zindex.api:app --host 127.0.0.1 --port 8000
# 或： make serve
```

### 4.4 一键干净目录端到端演示

```bash
bash scripts/serve_demo.sh             # make demo（可用 PORT=xxxx 改端口）
```

请求样例见 `examples/requests.sh`。

---

## 5. HTTP 接口摘要

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 引擎/版本与现有 schema |
| `POST /schemas` | 建 schema（`?overwrite=true` 为覆盖） |
| `PUT /schemas/{name}` | 替换已存在 schema（不存在→404，不静默创建；替换会清其 chunk） |
| `GET /schemas`, `/schemas/{name}` | 列表 / 详情（含每个 chunk 的码范围、字节数、路径） |
| `POST /schemas/{name}/ingest` | 直接灌原始整数行 |
| `POST /schemas/{name}/ingest_synthetic` | 灌确定性合成数据（`shape/seed/n/capacity`） |
| `POST /schemas/{name}/query` | 范围查询 `{lo,hi,max_intervals?,limit?}` |
| `POST /schemas/{name}/rewrite` | 全局按码重写/压实（row id 保留） |
| `GET /requests/{id}`, `GET /requests` | 请求审计 |

**统一响应信封**：`request_id`、`engine_version`、`status`
（`complete|degraded|error`）、`data`、`steps`、`stats`、`errors[]`、
`uncertainties[]`。失败原因（`errors`）与不确定结论（`uncertainties`）始终单列。
入站可带 `X-Request-ID`，否则自动生成；响应头与所有结构化日志都带该 id。

失败类别（HTTP 状态 + 稳定 category）：`schema_not_found`(404)、
`schema_exists`(409)、`invalid_schema`(400)、`invalid_coordinate`(400)、
`invalid_box`(400)、`chunk_not_found`(404)、`internal_error`(500)。
非致命不确定：`budget_exhausted`（扩大候选，零漏行）、`chunk_unreadable`
（响应 200 但 `status=degraded`，列出缺失/损坏 chunk 与路径）。

`steps` 对查询展示：`box_mapped`（原始边→无符号边）、`box_decomposed`
（区间数/精确数/保守数/预算/分裂胞腔数）、`chunks_pruned`（选中/跳过及 id）、
每个 chunk 一条 `chunk_scanned`（候选数、精确命中数、字节数、文件路径）。

---

## 6. 验证结果（本次如实记录）

- `pytest`：**41 passed**（`tests/test_encoding.py`、`tests/test_kernel.py`、
  `tests/test_api.py`）。
- `scripts/validate.py`：**overall PASS**，4 个场景（2D 16 位、4D 20 位稀疏、
  8D×16=**128 位**稀疏、混合有符号/无符号不等宽）全部零漏行零假行、往返无误、
  重写后 row id 稳定。完整数字见 `results/validation.json`。

代表性数字（节选自最近一次运行）：

- 8D 128 位 needle：预算 1 → 候选 6000、膨胀 5999、读 4/4 chunk；预算 4096 →
  候选 **1**、膨胀 **0**、读 **1/4** chunk；高位极值码往返正确。
- 4D 稀疏簇邻域：预算 1 → 候选 12000（膨胀 7×/1500 真命中）；预算 4096 →
  候选 1740（膨胀 0.16）、只读 3/6 chunk。
- 2D 薄 sliver：预算 1 → 候选 20000（膨胀上百倍）；预算 256 → 候选=真命中、
  读 2/5 chunk。候选集随预算单调收紧，物理读取字节随选中 chunk 数下降。

> 这些结果是**实际执行后记录**的；机器可复核：`make test && make validate`。
