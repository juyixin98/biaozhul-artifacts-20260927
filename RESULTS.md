# 验收执行结果（RESULTS）

本文件记录在干净目录按 `README.md` 步骤实际执行的结果。环境：Linux
x86_64，Python 3.12，依赖固定版本见 `requirements.txt` / `pyproject.toml`。

## 1. 依赖安装

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# pyarrow 17.0.0 / fastparquet 2024.11.0 / fastapi 0.115.2 /
# uvicorn 0.30.6 / pydantic 2.9.2 / pytest 8.3.3 安装成功
```

## 2. 测试套件

命令：`python -m pytest -q`

结果：

```
267 passed, 3 warnings in 2.13s
```

3 条 warning 均来自第三方库（fastparquet/starlette 的弃用提示），与本项目
逻辑无关。

测试文件与断言要点：

* `test_schema_admission.py` — 支持面、max D/R 元数据、空 struct、legacy
  2-level list、MAP/DECIMAL/TIMESTAMP/UUID/JSON 等明确拒绝、重名字段拒绝。
* `test_kernel_levels.py` — 手写 D/R 真值表（`fixtures/expected_trees.json`）、
  空列表/NULL 列表/列表内 NULL 三分、3 层 list、连续 NULL、NULL 结构元素按
  PyArrow 物化语义归一化比对。
* `test_paging.py` — 每页首槽 R=0、记录不丢失不重复、超大单记录不跨页
  （oversized 标记）、手工构造的“页中截断记录”计划被
  `PAGE_TRUNCATES_RECORD` 拒绝并定位到页号。
* `test_levels_codec.py` — RLE/bit-packed hybrid 自洽（200 seed × 多位宽）、
  v1 长度前缀、空流、以及与 fastparquet 解码器的互认。
* `test_differential.py` — 多 seed 随机 `list<list<int>>` 与 PyArrow 值一致；
  全步骤 PASS；1500 行跨页 fastparquet D/R 一致且每页 R=0；手写错误预期触发
  `ROUNDTRIP_MISMATCH`；页报告含记录范围。
* `test_kernel_errors.py` — int32 越界、类型不符、required 字段 NULL、list
  容器类型错误、记录非对象、未知 struct 字段、列 R=0 计数不一致。
* `test_api.py` — FastAPI TestClient：健康检查、verify 200/OK、request-id
  关联、SQLite run+events 持久化、`include=values,levels`、MAP/空 struct 的
  结构化 400、未知 run 404、schema 预览、run 列表。

## 3. 跨页大数据脚本

命令：`python scripts/generate_and_verify.py --records 1000 --page-bytes 128`

结果：

```
status: OK
  step kernel_encode: PASS
  step kernel_decode: PASS
  step expected_tree: PASS
  step kernel_paging: PASS
  step pyarrow_write_read: PASS
  step pyarrow_value_oracle: PASS
  step fastparquet_level_oracle: PASS
external pages per leaf: {'id': 1, 'matrix.list.element.list.element': 7,
                          'flags.list.element.tag': 4,
                          'flags.list.element.on': 3}
```

4000 行、3 列不同形状（含 3 层 list、list<struct>）的额外压力运行
（`--records 4000 --page-bytes 128`）同样 `status: OK`，外部文件产生多页
（例如 tri 三层叶子 11 页、内层 struct 叶子 23/24 页），fastparquet 核验每
页首槽 R=0、D/R 与内核完全一致。

## 4. HTTP 服务端到端

启动 `uvicorn app.main:app`，对 `fixtures/request_example.json` 发
`POST /api/v1/verify`：

```
status: UNCERTAIN
steps: kernel_encode/kernel_decode/expected_tree/kernel_paging/
       pyarrow_write_read/pyarrow_value_oracle/fastparquet_level_oracle : PASS
findings:
  [UNCERTAIN] DIALECT_NULL_STRUCT
  [UNCERTAIN] DIALECT_FASTPARQUET_NULL_STRUCT_D
```

无任何 FATAL；`UNCERTAIN` 来自示例中刻意包含的 NULL 顶层 struct
（`address: null`）、NULL list（`events: null`）以及 list 内 NULL 结构元素。
这两类是已记录的跨实现方言：物理 D/R 与值级三方一致，仅 struct 空值的对象
身份与 fastparquet 对独立 struct 的 D 读取不同，因此单列、不当作失败。

`GET /api/v1/runs/{id}` 可取回 run（status=UNCERTAIN）与其 10 条结构化事件。

## 5. 失败路径与失败类别（已在测试中固定）

| 触发 | 错误码 | HTTP |
| --- | --- | --- |
| MAP / DECIMAL / DATE / TIMESTAMP / UUID / JSON / ENUM / float32 等 | `UNSUPPORTED_LOGICAL_TYPE` | 400 |
| 零字段 struct | `EMPTY_STRUCT` | 400 |
| legacy 2-level repeated | `LEGACY_LIST_LAYOUT` | 400 |
| schema 字段缺 type / 重名 / list 缺 item | `INVALID_SCHEMA` | 400 |
| int32 越界 | `VALUE_OUT_OF_RANGE` | 400 |
| 值类型不符 / 记录非对象 / 未知 struct 字段 | `VALUE_TYPE_MISMATCH` | 400 |
| required 字段收到 NULL | `NULL_IN_REQUIRED` | 400 |
| 列 R=0 记录起点数不一致 | `COLUMN_RECORD_BOUNDARY_MISMATCH` | 422 |
| 页首槽 R!=0（页截断记录） | `PAGE_TRUNCATES_RECORD` | 422 |
| 内核↔手写预期 / ↔PyArrow / ↔fastparquet 不一致 | `ROUNDTRIP_MISMATCH` / `REFERENCE_MISMATCH` | 422 |

所有错误位置都携带列名、record/list_index、page、slot 等定位信息，并与
request-id 一起写入结构化日志与 SQLite。
