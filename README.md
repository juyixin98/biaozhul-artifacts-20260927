# arrowzero — 可审查的 Arrow 零拷贝列视图实现

安全地**导入、切片、拼接** Arrow 列缓冲区（定宽基本类型 + 变长 utf8 字符串），
对**有效位图（validity）、偏移（offset）、数据缓冲长度（data）分别独立验证**，
并记录每一步的零拷贝证据与**实际复制字节数**。

技术栈：Python 3.10+ · PyArrow · FastAPI · NumPy（仅用于内核中的字节视图与安全类型转换）· SQLite。
所有输入均为本地合成夹具，不需要任何生产账号或真实业务数据。

## 设计约束（与验收点对应）

- 切片是**纯零拷贝**：同一组底层 buffer，仅改逻辑 `offset/length`；`offset ≠ 0`
  时 NULL 判断与索引仍以 `offset + i` 为基准（见 `kernel/bitmap.py`、`kernel/view.py`）。
- 视图**持有底层所有权**：`ColumnView.buffers` 固定底层 `pyarrow.Buffer`；IPC
  导入时缓冲区位于 payload 内部，`_owners` 额外钉住 payload，释放源对象/reader
  后视图不读悬空内存（`tests/test_view.py` 中有释放后地址与取值断言）。
- 三层验证相互独立、失败按层归因：
  - validity：覆盖长度、尾部 padding 位必须为 0、null_count 一致性；
  - offsets：数量/长度、首偏移必须为 0（数组起点）、非递减、非负、不越界；
  - data：定宽按 `(offset+length)*width`、字符串按最终偏移量校验，并对非 NULL
    槽位做 UTF-8 良构性检查。
- 拼接是**物化复制**（不伪装成零拷贝），`CopyLedger` 逐 buffer 报告地址、大小、
  是否落在源 span 内、复制/分配字节数。
- **跨类型拼接默认拒绝**（`TYPE_MISMATCH`），必须显式 `cast_to`；不安全窄化转换
  （如 `int64→int16` 溢出、字符串↔数值）显式报错。
- 异常不会被统一返回成成功：服务审计状态只有 `committed / rejected / failed`，
  未知状态直接抛错。

## 工程结构

```
src/arrowzero/
  config.py          配置层（全部走环境变量，本地默认值）
  versions.py        运行时版本（健康检查/日志）
  observability.py   结构化 JSONL 日志（run_id、步骤、判定、版本）
  adapters.py        格式适配：pylist / IPC stream(零拷贝) / raw_buffers(故障注入)
  kernel/
    bitmap.py        位图原语（LSB-first、窗口计数、padding 检查）
    buffers.py       buffer span / overlap / 零拷贝证据
    checks.py        独立验证器（不导入内核、不依赖被测代码给答案）
    view.py          ColumnView：所有权 + 逻辑几何 + 零拷贝 slice
    concat.py        拼接内核 + CopyLedger + 显式 cast
  metadata.py        SQLite 元数据事务（业务行与审计行原子提交）
  service.py         服务编排：句柄注册表、事务、审计、失败分类
  api/               FastAPI 校验接口（schemas.py + app.py）
samples/             确定性合成夹具（生成器 + fixtures/fixtures.json）
scripts/verify.py    独立复核脚本（纯 Python oracle + PyArrow 双 oracle）
tests/               独立单元测试 + HTTP 集成测试 + 属性测试
```

## 首次使用（30 秒）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 运行全部测试
.venv/bin/python -m pytest

# 独立复核脚本（打印每个计算步骤、地址、实际复制量）
.venv/bin/python scripts/verify.py

# 启动服务
./run.sh
# 另一个终端：
./scripts/smoke.sh
```

## 真实测试命令与输出结论

### 单元 + 集成测试（pytest）

命令：`.venv/bin/python -m pytest`

结论（本机实测，Python 3.12.3 / pyarrow 25.0.1）：

```
85 passed in ~1.0s
```

测试分布：

| 文件 | 范围 |
|---|---|
| `tests/test_bitmap.py` | 位图 LSB 顺序、字节边界（7/8/15/16）、padding |
| `tests/test_checks.py` | 14 个用例，覆盖 validity/offsets/data 各层失败类别 |
| `tests/test_view.py` | 非零偏移 NULL/索引、空串 vs NULL、释放源后所有权 |
| `tests/test_concat.py` | 同类型/字符串/跨类型 cast、复制量、不安全窄化拒绝 |
| `tests/test_adapters.py` | pylist、IPC 零拷贝、raw 故障注入、坏 base64 |
| `tests/test_metadata.py` | 事务回滚、rejected 审计独立提交、非法状态拒绝 |
| `tests/test_service.py` | 端到端事务结果、失败分类、run_id 关联 |
| `tests/test_config.py` | 环境变量配置、默认值、非法值拒绝、不可变性 |
| `tests/test_fixture_artifact.py` | 磁盘上的样例夹具本身可加载且违规码符合预期 |
| `tests/test_api.py` | HTTP 集成：422/404/200 状态码、IPC API、审计可查 |
| `tests/test_property.py` | 随机数组对纯 Python oracle 与 PyArrow 双重比对 |

### 独立复核脚本

命令：`.venv/bin/python scripts/verify.py`

结论（实测，run_id 每次运行变化，判定结果稳定）：

```
==== SUMMARY: 47/47 passed ====
  ALL CHECKS PASSED   (exit code 0)
```

脚本覆盖八个部分：非零偏移切片、位图字节边界、空串/NULL、非法递减偏移（并交叉
验证 PyArrow 自己的 `validate(full=True)` 同样拒绝）、三层长度分别校验、释放源
对象后的悬空内存检查、跨类型 cast 与逐 buffer 复制量、多字节 UTF-8 拼接。

关键实测片段（地址与复制量每次运行会变，语义可复现）：

- 非零偏移：`slice(offset=1,length=5)` → python oracle、内核、pyarrow 三者均为
  `[None, '', 'βγ', '', None]`，且三组 buffer 对象地址完全一致（零拷贝）。
- IPC 所有权：`ipc import is zero-copy`；释放 payload 后
  `addresses before == addresses after`，取值仍正确。
- 跨类型拼接：默认 `TYPE_MISMATCH`；显式 `cast_to=int64` 后 5 个元素物化，
  实测 `copied_bytes=41`（40 数据 + 1 位图），所有输出 buffer 的
  `aliased_source=null`（不别名任何源）。
- 非法递减偏移 `[0,3,2,5]`：`DECREASING_OFFSET[offsets]@2`，PyArrow
  `ArrowInvalid: Offset invariant failure: non-monotonic offset at slot 2`。

## HTTP 接口

| 方法 & 路径 | 作用 |
|---|---|
| `GET /healthz` | 版本与存活状态 |
| `POST /api/v1/arrays/import` | `pylist` / `ipc_stream`(base64) / `raw_buffers` 导入 |
| `POST /api/v1/arrays/slice` | 零拷贝切片，返回 buffer 同一性与 `copied_bytes=0` |
| `POST /api/v1/arrays/concat` | 拼接；`cast_to` 显式跨类型转换；返回复制账本 |
| `POST /api/v1/arrays/values` | 取具体值与几何描述 |
| `POST /api/v1/arrays/export` | IPC stream（base64）回传 |
| `POST /api/v1/validate` | 只验证不导入，返回具体违规码/层/下标 |
| `GET /api/v1/runs/{run_id}` | 该 run 的操作审计与已注册数组 |

所有端点接受 `x-run-id` 请求头（也可在 body 内带 `run_id`），同一 run 的操作在
SQLite 与 JSONL 日志中可关联。

### 失败语义（不是"能调通"即可）

- `422`：请求形态错误或缓冲体验证失败，error 体带精确 `code`（如
  `DECREASING_OFFSET`、`TYPE_MISMATCH`、`SLICE_RANGE`）；
- `404`：未知句柄；
- `500`：未预期异常（状态记为 `failed`，绝不与成功混淆）。

## 复制量是怎么算的

- **slice**：不产生新 buffer，`copied_bytes = 0`，并逐 buffer 断言 `a is b`。
- **concat**：为 validity/offsets/data 各做一次精确大小的新分配；
  `CopyLedger` 把每个输出 buffer 的地址区间与全部源 span 比较，落在源内为
  "别名"（不计复制），否则计入复制；同时输出 Arrow 分配器前后差值
  `allocator_delta_bytes` 作为旁证。字符串输出 data 长度按各 chunk
  `[lo, hi)` 窗口精确预算，NULL 槽位零宽度、不拷贝。

## 配置

复制 `.env.example` 的键值或直接用环境变量（默认值即开箱可跑）：

| 变量 | 默认 |
|---|---|
| `ARROWZERO_DB_PATH` | `data/arrowzero.db` |
| `ARROWZERO_LOG_PATH` | `logs/arrowzero.jsonl` |
| `ARROWZERO_REGISTRY_CAPACITY` | `128` |
| `ARROWZERO_HOST` / `ARROWZERO_PORT` | `127.0.0.1` / `8000` |

更多设计细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
