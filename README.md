# Arrow 零拷贝视图服务（Zero-Copy Column Views）

一个**可审查**的 Arrow 列缓冲区实现：安全导入（import）、零拷贝切片（slice）、
以及带精确复制量计数的拼接（concat）。支持 10 种定长基本类型与变长 UTF-8 字符串。
数据全部来自本地合成夹具（synthetic fixtures），不依赖任何生产账号或真实业务数据。

技术栈：**Python 3.10+ · FastAPI · PyArrow · SQLite · pytest**。

---

## 1. 它解决什么 / 关键安全点

外部提交的是**原始、不可信**的 Arrow 缓冲区字节（validity 位图 / int32 偏移 / data）。
本工程把三类缓冲区**分别独立校验**，再零拷贝导入：

| 缓冲区 | 独立检查项 | 失败类别（`category`） |
|---|---|---|
| **validity 位图** | 字节数是否覆盖逻辑跨度；尾部填充位必须为 0；声称的 `null_count` 与位图实际一致 | `validity_too_short` / `validity_trailing_bits_set` / `null_count_mismatch` |
| **offsets（int32）** | 是否存在、4 字节对齐、长度覆盖 `length+1`、首偏移为 0、非负、**单调不减**、末偏移不越界 | `missing_buffer` / `buffer_not_aligned` / `offsets_too_short` / `offset_not_zero` / `offset_out_of_range` / **`offsets_not_monotonic`** |
| **data** | 是否存在、按元素宽度对齐、字节长度覆盖所有元素；字符串再做逐元素 UTF-8 语义解码 | `data_too_short` / `buffer_not_aligned` / `semantic_scan_failed` |

其余保证：

* **非零 offset 切片的 NULL 判断与索引准确**：位图按物理位 `offset + i` 读取，
  跨越字节边界（如 offset=7）仍正确；切片结果与 PyArrow 逐一比对。
* **零拷贝视图持有底层所有权**：`ColumnView` 强引用 `pa.Array` 与每个 `pa.Buffer`；
  释放导入时的源对象后，视图读取不会悬空（见 `tests/unit/test_zero_copy.py`）。
* **拼接必然复制**（Arrow 连续性要求），内核**逐字节计数**真实复制量并返回；
  位图按逻辑位合并而非原始字节拼接。
* **跨类型拼接必须显式转换**：不传 `target_type` 直接返回 `type_mismatch`；
  传入后执行显式 cast，并单独计其分配字节。
* **不把异常/未知状态统一返回成功**：每个失败都有稳定类别；未预期异常记为
  `internal_error`（HTTP 500），绝不静默成 200。

---

## 2. 工程结构（按四层 + 配置/测试组织）

```
app/
  config.py                 配置层：config/default.toml + APP_* 环境变量覆盖
  errors.py                 错误类别枚举 + LayoutError（稳定 category）
  logging_setup.py          结构化日志：run_id 与 input_fp（输入指纹）关联
  core/                     【执行内核】
    types.py                支持的类型表（规范名 + utf8/float32/float64 别名）
    bitmath.py              纯 Python LSB0 位图数学（独立判断，不借 PyArrow）
    layout.py               RawColumnBuffers：跨越信任边界的原始缓冲区描述
    columnview.py           ColumnView：持有所有权的零拷贝视图（offset/length）
    concat.py               拼接内核：位图逻辑合并、精确复制量 CopyReport、显式 cast
  validation/
    checks.py               【验证】validity/offsets/data/schema/semantic 独立检查
  adapters/                 【格式适配】
    descriptor.py           JSON+base64 描述符 -> RawColumnBuffers
    importer.py             安全导入（含导入前强制校验 + 与 PyArrow 三方交叉核对）
  store/
    metadata.py             【元数据事务】SQLite：可重入事务/savepoint、操作审计
  service/service.py        编排：校验 -> 导入 -> 切片 -> 拼接 -> 元数据
  api/                      【验证接口】FastAPI：schema、路由、分类错误处理
tests/
  fixtures/oracle.py        独立预言机：用 struct 手工构造期望答案（不经被测核心）
  unit/                     74 个单元测试
  integration/              11 个 FastAPI 全链路集成测试
examples/                   样例数据（含一个故意非法的偏移夹具）
scripts/demo.py             端到端演示（覆盖全部评审场景）
scripts/curl_example.sh     真实 HTTP 走查
config/default.toml         启动配置
run.py                      启动入口
```

---

## 3. 首次使用：安装与运行

```bash
# 1) 创建虚拟环境并安装依赖
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2) 运行全部测试（真实命令）
python -m pytest

# 3) 跑端到端合成演示（无需起服务）
python scripts/demo.py

# 4) 启动服务（默认 127.0.0.1:8000；可用 APP_PORT 覆盖）
python run.py
# 或：uvicorn app.api.app:app --host 127.0.0.1 --port 8000
```

### 真实测试结论（本机实测）

环境：Linux，Python 3.12.3，pyarrow 25.0.1，fastapi 0.141.1，pytest 9.1.1。

```text
$ python -m pytest
======================== 85 passed, 1 warning in 0.87s =========================

$ python -m pytest tests/unit -q
============================== 74 passed in 0.47s ==============================
$ python -m pytest tests/integration -q
============================== 11 passed, 1 warning in 0.68s ==================
```

日志示例（带运行身份与输入指纹，可关联到具体输入）：

```text
09:48 INFO app.api [run=run-slice fp=3f9a…c1] POST /columns/col_x/slice incoming (28 body bytes)
09:48 INFO app.service slice done: col_x[3:8] -> col_y zero_copy={'data':True,'validity':True}
```

> 注：`1 warning` 来自 starlette TestClient 对 `httpx` 的弃用提示，与功能无关。

---

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 版本与支持类型 |
| POST | `/validate` | **只校验不入库**，返回五组检查与失败类别 |
| POST | `/columns` | 校验通过后零拷贝导入 |
| POST | `/columns/import-ipc` | 导入 Arrow IPC 流的某一列 |
| POST | `/columns/{id}/slice` | 零拷贝切片 `{offset,length}` |
| POST | `/columns/concat` | 拼接；跨类型需 `"target_type"` 显式转换 |
| GET | `/columns` · `/columns/{id}` | 列出 / 查看（含 values、null_flags、缓冲区地址） |
| DELETE | `/columns/{id}` | 删除 |

请求描述符（JSON，字段为标准 base64）：

```json
{
  "type": "int32",
  "length": 10,
  "validity": "<base64 位图，省略=全部有效>",
  "data": "<base64 定宽数据>",
  "null_count": 2
}
```

字符串类型额外需要 `"offsets"`（`length+1` 个小端 int32）。可传
`X-Run-Id` 头；响应与日志都会回显该 id，并附带 `X-Input-Fp`（请求体 sha256 前缀）。

### 实测关键响应（节选自真实走查）

非零 offset 切片（offset=3 跨越第一个位图字节），与父列共享同一缓冲区、零复制：

```json
"offset": 3, "length": 4, "null_count": 2,
"null_flags": [true, false, false, true],
"slice": { "buffers_shared_with_parent": {"validity": true, "data": true},
           "copied_bytes": {"total": 0}, "zero_copy": true }
```

非法递减偏移：

```text
ok: False
categories: ['offsets_not_monotonic']
message: offset[2]=1 < offset[1]=3 (decreasing offsets)
evidence: {'index': 2, 'previous': 3, 'value': 1, 'offsets_head': [0, 3, 1, 6]}
```

跨类型拼接：

```text
no-target  HTTP 422  category: type_mismatch
target=int64 HTTP 200 values: [1, 2, 3, 4]
copied_bytes: {"validity":0,"offsets":0,"data":32,"cast":16,"total":48}
```

启动后可运行完整走查：

```bash
python run.py            # 终端 A
./scripts/curl_example.sh   # 终端 B（BASE 默认 http://127.0.0.1:8000）
```

---

## 5. 评审场景如何被断言（测试不是“接口能调通”而已）

- **非零偏移切片**：`test_slice_nonzero_offset_nulls_and_indices_primitive`、
  `test_slice_across_bitmap_byte_boundary_offset_7`、`test_slice_of_slice_string_nonzero_offset`，
  同时断言 values、逐位 `is_null`、`offset` 字段、与 PyArrow `slice(...).to_pylist()` 一致、
  以及缓冲区指针相等。
- **位图字节边界**：`test_packed_bitmap_roundtrip_every_length`（长度 0..17 全覆盖）、
  `test_bitmap_byte_boundary_nulls_every_8th_slot`、尾部填充位被置位时
  返回 `validity_trailing_bits_set`。
- **空串与 NULL**：`test_empty_string_is_valid_zero_span_not_null` 断言
  空串是零宽度**有效**值、NULL 才占位图 0，二者计数分开。
- **非法递减偏移**：`test_decreasing_offsets_detected_with_index_and_values`
  断言具体索引、前后值和类别；`from_buffers` 本身**不校验**单调性（实测会在
  读取时抛 SystemError），因此必须由本层独立拦截。
- **与 PyArrow 一致 + 真实复制量**：每个拼接用例同时断言 Python 独立预言机、
  PyArrow `concat_arrays` 结果，以及 `bytes_validity/offsets/data/cast` 的**具体字节数**。
- **所有权**：`test_view_remains_valid_after_source_objects_released` 在局部源对象
  释放并 `gc.collect()` 后仍能正确读取和再切片。
- **独立答案**：`tests/fixtures/oracle.py` 用标准库 `struct` 手工打包/解包，
  期望值**不由被测核心产生**。

---

## 6. 配置

`config/default.toml` 为默认值，所有键可用 `APP_<大写键名>` 覆盖，例如
`APP_PORT=8077`、`APP_DATABASE_PATH=/tmp/meta.db`、`APP_LOG_FORMAT=prod`（JSON 日志）。

## 7. 范围说明

- 支持：`int8/uint8/int16/uint16/int32/uint32/int64/uint64/float/double`（亦接受
  `float32/float64` 别名）与 `string`（亦接受 `utf8`）。
- 明确**不支持** `bool`（位打包 data 规则不同，会混淆独立 data 检查）与
  `large_string`（64 位偏移，偏移契约不同）；提交即返回 `unsupported_type`。
- 定长类型按小端解码。
