# 多列批次字典编码统一服务 (dicunify)

把多个列批次（column batches）各自的**局部字典**统一为一个**全局字典**，并输出
每批“局部编号 → 全局编号”的重映射。技术栈：Python 3.12 + FastAPI + PyArrow +
SQLite；全部数据为本地合成夹具，不依赖任何生产账号或真实业务数据。

---

## 1. 语义与支持范围

### 输入批次

每个批次包含：

| 字段 | 含义 |
| --- | --- |
| `batch_id` | 请求内唯一的批次标识 |
| `dictionary` | 局部字典值列表；**局部编号 = 列表下标** |
| `indices` | 每行引用的局部编号 |
| `validity` | 每行有效位图（可省略，省略时全行有效） |

支持的值类型（`value_type`）：`string`、`int64`、`double`、`bool`。

### 核心规则（对应任务约束）

1. **局部索引宽度与有效位图独立处理，NULL 不占用普通值语义。**
   NULL 只由 `validity=false` 表达；NULL 行的索引槽位使用中性填充值 `0`，
   解码时**绝不**查字典。字典值列表中出现 NULL 会被拒绝
   （`NULL_DICTIONARY_ENTRY`）。范围校验只对有效行执行，因此“空字典 + 全
   NULL 行”是合法输入。

2. **同值不同局部编号必须合并；同编号不同值绝不能误合并。**
   合并以**值**为键，而不是以局部编号为键。例如批次 A 的 `0="a"` 与批次 B 的
   `2="a"` 合并为同一全局项；A 的 `0="x"` 与 B 的 `0="z"` 保持为两个全局项。

3. **字典排序政策固定**：`typed-ascending-v1` —— 按请求声明的值类型升序
   （字符串按 Unicode 码位）。策略标识会随每个作业持久化；改动该策略属于
   破坏性元数据变更。`double` 类型拒绝 `NaN`（破坏全序与等值合并）。

4. **超过目标索引位宽时明确扩容或拒绝**：
   - `index_policy="auto"`：在 `uint8 / uint16 / uint32` 中选能容纳基数的
     最小宽度；基数为 0 时固定返回 `uint8`（无值编码，仅 NULL）。
   - `index_policy="strict"` + `target_width`：基数超过
     `2^8-1 / 2^16-1 / 2^32-1` 时返回 **422 `INDEX_WIDTH_OVERFLOW`**，
     响应里给出 `cardinality / requested_width / capacity`。
   - 超过服务硬上限（默认 `2^32-1`，可用 `DICUNIFY_MAX_CARDINALITY` 调小
     用于测试）返回 **422 `CARDINALITY_LIMIT_EXCEEDED`**。

5. **局部字典内重复值**：内核默认拒绝
   （`DUPLICATE_VALUE_IN_DICTIONARY`，错误明细给出 `first_code` 与
   `duplicate_code`）；JSON 接口可用 `dedupe_local_dictionary=true` 选择
   规范化（首次出现的编号胜出，索引随之改写），响应 `normalization` 中
   报告被合并的重复项及明细。

### “重映射不依赖输入顺序”的声明范围（重要边界）

在固定排序策略下，以下结论成立，并有专门测试
（`tests/test_order_independence.py`）：

- **成立**：全局字典内容、全局编号、每行解码结果，在
  (a) 批次提交顺序任意排列、(b) 局部字典编号任意置换 下保持不变。
- **不成立/不覆盖**：
  - `batch_remaps` 数组的**次序**——它按提交顺序排列（这是有意的输出约定）；
  - 局部字典含重复项且开启规范化——按合同局部编号会改变（首次出现者胜）；
  - 声明的 `value_type` 不同或值集合本身不同（不同数据当然不同结果）；
  - 排序策略变化。

### 失败类别（不会被统一返回成功）

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 400 | `EMPTY_REQUEST` | 没有批次 |
| 400 | `DUPLICATE_BATCH_ID` | 批次 id 重复 |
| 400 | `UNSUPPORTED_VALUE_TYPE` | 值类型不在支持集合 / Arrow 类型不匹配 |
| 400 | `NULL_DICTIONARY_ENTRY` | 字典值列表含 NULL（应走位图） |
| 400 | `VALUE_TYPE_MISMATCH` | 标量类型不符、int64 越界、double 为 NaN |
| 400 | `DUPLICATE_VALUE_IN_DICTIONARY` | 局部字典同值多编号（未开启规范化） |
| 400 | `INVALID_INDEX` | 索引为负 / 非整数（布尔、浮点不算） |
| 400 | `INDEX_OUT_OF_RANGE` | 有效行的索引超出局部字典 |
| 400 | `INVALID_VALIDITY` | 位图长度/元素类型不符 |
| 422 | `INDEX_WIDTH_OVERFLOW` | strict 宽度装不下基数 |
| 422 | `CARDINALITY_LIMIT_EXCEEDED` | 超过服务硬上限 |
| 500 | `VERIFICATION_MISMATCH` | 独立往返校验发现解码与原批不一致 |
| 500 | `INTERNAL_ERROR` | 未预期异常（同样落库为 FAILED） |

成功/失败都在 SQLite 中留下作业行：`RUNNING → SUCCEEDED|FAILED`，
终态不可改写（异常路径绝不会把失败记成成功）。

---

## 2. 工程结构

```
app/
  config.py                 # 配置层：环境变量、宽度容量、排序策略常量
  core/
    kernel.py               # 执行内核：纯 Python 统一逻辑（无 FastAPI/Arrow/SQLite）
    verify.py               # 独立验证：自成体系的解码与逐行比对（不复用内核合并代码）
    errors.py               # 失败类别（稳定 code + HTTP 映射）
  adapters/
    jsonio.py               # 格式适配：JSON/dict <-> 内核对象、重复项规范化
    arrowio.py              # 格式适配：Arrow IPC（DictionaryArray，uint8/16/32 索引）
  store/
    sqlite_store.py         # 元数据事务：RUNNING/SUCCEEDED/FAILED 状态机
  service/
    engine.py               # 编排：解析 -> 内核 -> 独立校验 -> 事务提交
    identity.py             # run/job id、组件版本
    logging_setup.py        # JSONL 结构化日志（关联 run_id/job_id/step）
  api/
    schemas.py              # Pydantic 线模型
    app.py                  # FastAPI 路由
tests/
  oracle.py                 # 独立参考实现（不 import 被测内核，自行给出期望结果）
  fixtures.py / conftest.py # 本地合成夹具 + 运行身份/进度/产物记录
  test_kernel.py            # 重复字典/空字典/全NULL/宽度阈值/位图/具体失败类别
  test_order_independence.py# 顺序无关声明的成立范围与反例
  test_api.py               # FastAPI 端到端、落库状态、/verify 成功与抓篡改
  test_arrow.py             # Arrow IPC 适配 + 随机属性测试对拍 oracle
  test_store.py             # 事务回滚/终态不可覆盖/schema 版本
  test_json_adapter.py      # 线级类型强转与规范化
examples/                   # 示例请求与本地 in-process 演示
```

测试**不是**“接口能调用即可”：每个测试断言具体全局字典、具体编号、具体
逐行解码值以及具体失败类别；期望值由 `tests/oracle.py` 中独立的第二份实现
和手写常量共同给出，参考答案不是被测内核自己生成的。

---

## 3. 本地启动

需要 Python 3.10+（开发于 3.12）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # 运行时（锁定全树）
# 运行测试还需要：
.venv/bin/pip install -r requirements-dev.txt
```

启动服务（默认监听 127.0.0.1:8000，SQLite 位于 data/dicunify.db）：

```bash
.venv/bin/python -m uvicorn app.api.app:app --host 127.0.0.1 --port 8000
# 或
.venv/bin/python -m app
```

配置（环境变量）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DICUNIFY_DB_PATH` | `data/dicunify.db` | SQLite 文件路径 |
| `DICUNIFY_MAX_CARDINALITY` | `4294967295` | 硬上限，可调小以测试拒绝路径 |
| `DICUNIFY_LOG_LEVEL` | `INFO` | `DEBUG/INFO/WARNING/ERROR` |

### 示例请求（JSON）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/unify \
  -H 'Content-Type: application/json' \
  -d @examples/unify.json
```

要点：批次 `batch-1` 里 `"apple"` 的局部编号是 2（`batch-0` 里是 0），
`"banana"` 两边编号也不同；输出全局字典固定为
`["apple","banana","cherry"]`，两批分别给出 `local_to_global` 映射，
NULL 行索引填充 0 且 `validity=false`。

重复字典项 + strict 宽度示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/unify \
  -H 'Content-Type: application/json' \
  -d @examples/unify-duplicates.json
```

不启动服务器的本地演示：

```bash
.venv/bin/python examples/demo.py
```

### Arrow IPC 接口

`POST /api/v1/unify/arrow?value_type=string`（`index_policy`、
`target_width` 同为查询参数），请求体为单列、字典编码的 Arrow IPC stream；
NULL 编码为**索引数组上的 null 位图**（不是字典值里的 NULL）。响应是
Arrow IPC stream：每个输入批次一列（列名为 batch_id），共享同一全局字典，
索引类型为选中的 `uint8/uint16/uint32`。`tests/test_arrow.py` 与
`examples/demo.py` 展示了完整收发方式。

### 其他端点

- `GET /health`：服务状态与 python/pyarrow/fastapi/pydantic 版本。
- `GET /api/v1/jobs/{job_id}`：作业状态、统计、失败码/明细、批摘要、版本。
- `POST /api/v1/verify`：无状态独立校验——提交全局字典、各批原始字典/索引
  与重映射结果，服务用 `core/verify.py` 逐行解码比对；篡改任一会得到
  500 `VERIFICATION_MISMATCH`。

---

## 4. 运行测试

```bash
.venv/bin/python -m pytest
```

每次运行生成 `test-runs/run-<时间戳>-<短uuid>/`：

- `run-info.json`：run id、python/pyarrow/fastapi/pydantic 版本、cwd、
  git commit、开始时间；
- `steps.jsonl`：测试内记录的输入与计算步骤（基数、宽度、逐行期望值等）；
- `results.jsonl`：每个用例 setup/call/teardown 的结果、耗时，失败时附
  断言尾部与 stdout；skip 会写明原因——**未执行项不会被伪装成通过**；
- JUnit XML：`test-runs/last-junit.xml`。

失败时可凭日志中的 `run_id / job_id / batch_id / row` 直接关联到输入批次、
行号和判定依据（容量、基数、期望/实际 repr）。

### 失败/未执行项（如实记录）

- 1 个用例**有意保留为 SKIPPED**：
  `test_huge_cardinality_hard_cap_skip` —— 真正构造 `2^32+1` 个不同字符串
  在 CI 不现实；同一条拒绝路径由可配置上限
  （`test_hard_cardinality_limit_rejected` 用 `max_cardinality=9`）覆盖，
  skip 原因写入用例与 `results.jsonl`。
- 其余用例当前全部通过；开发过程中真实出现并修复的问题包括：
  anyio 新版本与 starlette 的弃用冲突（锁定 `anyio==4.6.2.post1`）、
  SQLite `executescript` 隐式提交导致建表事务失败、空字典+全NULL行占位
  索引被误判越界。

---

## 5. 关键取舍

- **内核零框架依赖**：统一逻辑只用纯 Python 值，便于直接推理与对拍；
  FastAPI/PyArrow/SQLite 全部在外层。
- **独立验证器与内核不共享合并代码**：`core/verify.py` 仅根据结果自身解码，
  并额外强制 NULL 填充必须为 0、全局编码不得越界。
- **uint8 用于基数 0**：空字典时没有任何值编码，选最小的规范 Arrow 索引
  类型，避免“没有宽度”这种未知状态。
- **只到 uint32**：Arrow 字典索引对 uint64 支持有限且无真实需求；超限明确
  拒绝而不是悄悄截断。
- **重复局部字典项默认报错、显式选择才规范化**：避免内核擅自决定哪个编号
  胜出；规范化规则固定为“首次出现者胜”并回报明细。
- **只持久化元数据，不持久化数据载荷**：SQLite 记录状态、统计、失败类别、
  版本与每批行数/NULL 数；`/verify` 为无状态接口，由调用方回传结果。
