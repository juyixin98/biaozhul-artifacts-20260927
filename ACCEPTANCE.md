# 验收结果记录（ACCEPTANCE REPORT）

本文件如实记录在干净环境下的实际执行结果。日期：2026-09-28。

## 环境

- OS：Linux 6.8.0-90-generic (Ubuntu)，Python **3.12.3**
- 依赖（`requirements.txt` 固定版本）：

  | 包 | 版本 |
  |---|---|
  | pyarrow | 18.1.0 |
  | fastapi | 0.115.6 |
  | uvicorn | 0.34.0 |
  | pydantic | 2.10.4 |
  | httpx | 0.28.1 |
  | pytest | 8.3.4 |

- 无生产账号 / 无外部服务；数据均为本地合成夹具。

## 复现步骤与实测结果

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m pytest tests/ -v
```

**结果：`66 passed, 1 warning in 1.21s`**（warning 为 starlette 对
anyio 别名的第三方弃用提示，与本项目代码无关），退出码 0。

测试分布（均为具体结果 / 失败类别断言，非“接口可调用”检查）：

- `test_schema.py`：max DL/RL 数值、7 种不支持逻辑类型逐一被显式拒绝、
  非法 schema、required NULL 拒绝。
- `test_levels_kernel.py`：逐记录手写 `(DL,RL,value)` 精确比对；
  `[[1,2]]`→RL`0,2` 与 `[[1],[2]]`→RL`0,1` 的歧义区分；NULL 列表 / 空列表 /
  列表内 NULL 三态；list-of-struct、struct-of-list、空结构 per-record 存在性；
  列记录边界失配检测（带列路径）。
- `test_encodings.py`：RLE 头偶位、bit-packed LSB 组与尾组填充、
  4 字节长度前缀、PLAIN 的 bool/int32/string/double、Thrift footer/page header
  compact 往返。
- `test_parquet_io.py`：自写自读嵌套；**PyArrow 读我们写的 Parquet**；
  **我们读 PyArrow 写的 Parquet**；300 条跨页记录重组；超宽记录不被页切分；
  全基元类型；Arrow 非 STRING 的 BINARY 被我们明确拒绝；逐页页号 / 记录区间。
- `test_fuzz_oracle.py`：6 个种子的随机多层嵌套数据，直接解码 PyArrow 文件的
  **物理 RL/DL 字节**，与内核逐条相等。
- `test_service.py`：通过 / 失败 / 错误三态、`EXPECTED_TREE_MISMATCH` 等具名
  类别、跨页大批量、空结构不确定性单列、SQLite 持久化（成功与失败均可查询）。
- `test_api.py`：HTTP 层健康检查、request_id 关联 / 自动生成、不支持类型 HTTP
  响应、未知请求 404、请求列表。

## 手工 HTTP 验收（uvicorn 实测）

- `POST /api/v1/validate`（`examples/validate_nested.json`，
  `force_page_after_records=2`）→ `status=passed`，9 个页、6 条记录；步骤含
  逐页布局；`oracle_roundtrip` 差异 0；`cross_interop` 双向均 `ok`；产出
  自研 666 字节与 PyArrow 1258 字节两个制品。
- `POST /api/v1/validate`（`examples/validate_unsupported.json`，decimal）→
  `status=error`，`error_category=UNSUPPORTED_LOGICAL_TYPE`，信息明确。
- 故意写错 expected_tree → `status=failed`，
  `EXPECTED_TREE_MISMATCH`，定位到 `record 5 / $.ids.length / expected 2 actual 1`。
- 空结构请求 → `status=passed`，`uncertainties` 单列其字节不可见性，
  `cross_interop` 标 `skipped`，内核树往返仍验证通过。
- 独立用 PyArrow 读取自研制品：识别为 `list<list:list<element:int32>>`，
  6 行树与输入一致。
- 结构化日志每行带 `request_id`，可按请求聚合；SQLite 中成功 / 失败请求均可
  通过 `GET /api/v1/requests/{id}` 复查。

## 已知限制（已在接口与文档中单列）

- 零字段 `struct<>` 无 Parquet 物理列：树级可验证，字节级 PyArrow 对照不可得，
  以不确定性而非失败呈现；PyArrow 自身也拒绝写这种结构。
- 仅支持 Parquet v1 数据页、PLAIN 值、RLE/bit-packed hybrid level、
  UNCOMPRESSED；其余明确拒绝。
