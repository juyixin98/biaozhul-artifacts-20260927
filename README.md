# dictsvc — 多列批次字典编码统一服务

把多个**局部字典编码**的列批次（column batch）合并成一个**全局字典**，
并输出每批从局部编号到全局编号的**重映射（remap）**。技术栈：
Python · FastAPI · PyArrow · SQLite，全部本地运行，不依赖任何生产账号或外部服务。

---

## 1. 它解决什么 / 不解决什么

每个输入批次都有自己的局部字典：`dictionary[局部编号] = 值`，行数据是一串
局部编号。不同批次里：

- **同一个值可能落在不同的局部编号**（例如批 b1 里 `"a"` 既是编号 0 又是编号
  2，批 b2 里 `"b"` 是编号 1）；
- **同一个局部编号可能代表不同的值**（b1 的编号 0 是 `"a"`，b2 的编号 0 是 `"z"`）。

服务按 **`(类型, 值)`** 做全局合并：

- 同值、不同局部编号 → **合并为同一个全局编号**；
- 同局部编号、不同值 → **绝不误合并**（全局字典绝不用局部编号做键）。

### 关键语义

1. **局部索引宽度与有效位图独立处理。** 每行有两个相互独立的结构：
   一个无符号整数索引（8/16/32/64 位阶梯）和一个有效位（valid bitmap）。
   **NULL 只由有效位表达，不占用任何字典值编号。** NULL 行的索引槽内容不会被
   解引用（即使槽里是越界数字也无所谓——测试明确覆盖了这一点）。
2. **字典中出现 NULL 值是错误**（`DICTIONARY_CONTAINS_NULL`），NULL 永远不能
   伪装成一个普通字典值。该规则在执行内核里强制，任何适配器都绕不过。
3. **字典排序政策固定**为 `type_then_value`：先按值类型（int64 排在 utf8 前），
   类型内 int64 按数值、utf8 按 UTF-8 字节序。政策随运行元数据持久化；
   不允许请求时改成别的政策（政策一变，所有编号都会变）。
4. **超位宽明确处理**：全局基数超过目标位宽容量时，
   `width_policy=reject`（默认）返回分类错误 `CARDINALITY_OVERFLOW`（错误详情给出
   所需位宽）；`width_policy=expand` 按固定阶梯 8→16→32→64 扩容；超过 64 位
   一律拒绝，不做静默截断。
5. **重复字典项**默认合并（并在统计中计数 `duplicate_declared`）；
   `on_duplicate_values=error` 时返回 `DUPLICATE_DICTIONARY_VALUE`。

### 「重映射不依赖输入顺序」声明的**适用范围**

测试核验的精确声明是：

- **批次数序**任意打乱、**批内行序**任意打乱，只要全局**值集合不变**，
  每个值的全局编号和每批 `local_to_global` 都不变（行上的编号随行移动）。
- **范围边界（刻意不承诺）**：全局编号是「排序后从 0 开始的位置」。
  一旦值集合增删，排序位置可能整体平移，编号稳定性**不**跨值集合保证。
  README 与测试都把这一边界写明，而不是含糊承诺「永远稳定」。

### 支持范围（明确的取舍）

| 维度 | 支持 | 不支持 / 取舍 |
|---|---|---|
| 字典值类型 | `int64`、`utf8`（二者可混用于同一运行，`1` 与 `"1"` 不会合并） | bool、float、二进制、嵌套/时间类型 |
| 整数范围 | int64 | 超出 int64 的大整数 → `UNSUPPORTED_VALUE_TYPE` |
| 索引位宽 | 无符号 8/16/32/64 阶梯 | 任意非阶梯位宽（如 12 位） |
| NULL | 仅有效位图 | 字典内 NULL |
| 全局字典交付 | JSON（异构类型无法放进一个 Arrow 列） | 单一 Arrow 字典列 |
| Arrow 批 | 一个 IPC 流、多个字典编码列；表是矩形的，不等长批次请用 NULL 行补齐或分多次请求 | 一个流里各列长度不同（Arrow 本身不允许） |
| 持久化 | SQLite 本地文件，运行/批次/全局条目/事件单事务原子提交 | 并发写同一 run_id（第二次返回 `RUN_CONFLICT`） |

---

## 2. 工程结构（按四层组织）

```
src/dictsvc/
  config.py                 # 配置层：环境变量、本地默认值、版本信息
  core/                     # 执行内核：纯 Python，零 Web/DB 依赖
    policy.py               #   固定政策：位宽阶梯、排序、NULL 哨兵
    model.py                #   数据模型：索引与位图相互独立
    encode.py               #   校验 + 全局合并 + 每批重映射（分类错误）
    decode.py               #   解码与往返验证（逐行）
    errors.py               #   失败类别（稳定的 error.category 契约）
  adapters/                 # 格式适配层：JSON 与 Arrow IPC
    json_in.py / json_out.py
    arrow_in.py / arrow_out.py
  metadata/                 # 元数据事务层：SQLite，显式事务边界
    store.py
  service/                  # 编排：内核 + 事务 + 验证 + 每运行日志
    orchestrator.py / logging_setup.py
  api/app.py                # 验证/接口层：FastAPI（/encode、/verify、/runs）
tests/
  oracle.py                 # 独立参考实现（不 import 任何被测核心代码）
  fixtures.py               # 本地合成夹具
  test_*.py                 # 断言具体结果与具体失败类别
examples/                   # curl/Python 示例请求
```

这不是单文件实现：内核不含固定返回值，API 不是唯一调用壳——内核可直接使用，
且所有语义在内核和接口两层都有测试。

---

## 3. 本地启动

需要 Python ≥ 3.10（开发于 3.12）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 完全锁定的依赖，见 requirements.txt
./run.sh                                     # http://127.0.0.1:8000
```

可用环境变量（均有本地默认值，见 `.env.example`）：
`DICTSVC_SQLITE_PATH`、`DICTSVC_LOG_DIR`、`DICTSVC_DEFAULT_WIDTH`、
`DICTSVC_DEFAULT_WIDTH_POLICY`、`PORT`。

交互式 API 文档：启动后访问 `http://127.0.0.1:8000/docs`。

---

## 4. 示例请求

### 4.1 JSON：重复字典项 + 跨批同局部编号

```bash
curl -s -X POST http://127.0.0.1:8000/v1/encode \
  -H 'content-type: application/json' \
  -d '{
    "run_id": "demo", "target_width": 8, "width_policy": "reject",
    "batches": [
      {"batch_id": "b1", "dictionary": ["a","b","a"],
       "indices": [0,1,2,0,null], "valid": [true,true,true,true,false]},
      {"batch_id": "b2", "dictionary": ["z","b"],
       "indices": [0,1,0], "valid": [true,true,true]}
    ]
  }'
```

关键结果（测试里全部硬编码断言）：

- 全局字典固定排序：`a→0, b→1, z→2`；
- b1：`local_to_global=[0,1,0]`（局部 0 和局部 2 同值合并），行
  `[0,1,0,0,null]`，位图 `[T,T,T,T,F]`；
- b2：`local_to_global=[2,1]`（它的局部 0 是 `z`，**没有**与 b1 的局部 0 混淆）；
- 响应内 `verification.all_match=true, checked_rows=8`（编码后逐行解码核对）。

### 4.2 空字典 / 全部 NULL

```bash
curl -s -X POST http://127.0.0.1:8000/v1/encode -H 'content-type: application/json' \
 -d '{"batches":[
   {"batch_id":"empty","dictionary":[],"indices":[],"valid":[]},
   {"batch_id":"all_null","dictionary":[],
    "indices":[null,null,null],"valid":[false,false,false]}]}'
```

→ 基数 0、位宽 8；NULL 行索引输出为 null、位图全 false，没有任何值编号被占用。

### 4.3 大基数阈值（256 正好 / 257 拒绝 / 257 扩容）

- 256 个不同值 + 8 位 → 成功，`global_index_width=8`；
- 257 个不同值 + 8 位 + reject → **422 `CARDINALITY_OVERFLOW`**，详情含
  `target_capacity=256, required_width=16`；
- 同样 257 + `width_policy=expand` → 成功，`global_index_width=16`。

### 4.4 Arrow IPC

```bash
.venv/bin/python examples/arrow_example.py        # 需要先 ./run.sh
```

请求体是 Arrow IPC 流（每列一个字典编码批次，NULL 用索引位图表示）；
响应 JSON 额外带 `arrow_remap_base64`：解码后是一张纵向表
（`batch_id, global_code, valid`），覆盖每一批重映射后的每一行。

### 4.5 独立验证接口

`POST /v1/verify` 不相信调用方，也不重新跑编码：它从 **SQLite 里持久化的
全局条目 + 每批全局索引 + 位图重新解码**，再与你提交的候选行逐行比对，
逐行给出 `VALUE_MISMATCH / ROW_COUNT / MISSING_BATCH / UNKNOWN_BATCH`。

```bash
curl -s -X POST http://127.0.0.1:8000/v1/verify -H 'content-type: application/json' \
 -d '{"run_id":"demo","batches":[
   {"batch_id":"b1","rows":["a","b","a","a",null]},
   {"batch_id":"b2","rows":["z","b","z"]}]}'
```

其它接口：`GET /healthz`、`GET /version`、`GET /v1/runs/{run_id}`（含策略、
统计、事件时间线、重映射）。完整脚本：`examples/requests.sh`。

---

## 5. 失败分类（绝不把异常/未知状态当成功）

| category | HTTP | 触发 |
|---|---|---|
| `REQUEST_MALFORMED` | 400 | JSON 结构/形状错误、非法选项、非 Arrow 流 |
| `DICTIONARY_CONTAINS_NULL` | 422 | 字典值槽位为 NULL |
| `INDEX_OUT_OF_RANGE` | 422 | **有效行**的局部索引越界（NULL 行不检查） |
| `DUPLICATE_BATCH_ID` | 422 | 同一请求两个批次同名 |
| `DUPLICATE_DICTIONARY_VALUE` | 422 | strict 模式下同值绑定多个局部编号 |
| `UNSUPPORTED_VALUE_TYPE` | 422 | 非 int64/utf8，或整数越界 |
| `CARDINALITY_OVERFLOW` | 422 | 超目标位宽（reject）或超过 64 位 |
| `RUN_CONFLICT` | 409 | run_id 已存在 |
| `RUN_NOT_FOUND` | 404 | 未知运行 |
| `INTERNAL_ERROR` | 500 | 未预期异常的兜底分类，同样不是成功 |

失败的运行也会落库（`status=failed` + 错误类别 + 事件），但成功运行不会被一次
冲突的失败写入覆盖（失败记录用 `INSERT OR IGNORE`）。

---

## 6. 元数据事务

`metadata/store.py` 用显式事务保证：一次成功运行的 `runs` 行、所有 `batches` 行、
`global_entries`、进度 `events` **一起提交**；任一约束失败整体回滚，
随后失败状态在**独立事务**中写入，因此永远读不到「半个成功」。重复 run_id 在
提交点触发 `RUN_CONFLICT` 并回滚（有专门测试核对批次/条目计数仍为 1）。

---

## 7. 测试如何回答你提出的具体问题

```bash
.venv/bin/python -m pytest tests/ -v
```

- **重复字典项**：`test_kernel_semantics.py::test_repeated_dict_items_*`、
  HTTP 版 `test_http_api.py`——断言 b1 `[0,1,0]`、b2 `[2,1]` 等**具体编号**，
  以及 strict 模式的具体失败类别。
- **空字典**：`test_empty_dictionary_*`——基数 0、宽度 8、空映射。
- **全部 NULL**：`test_empty_and_*` / `test_all_null_rows_*`——NULL 行索引
  不被解引用、声明但未使用的值计入 `unused_declared`、位图独立。
- **大基数阈值**：256 适配 8 位、257 reject 且详情 `required_width=16`、
  257 expand 到 16（具体位宽断言）。
- **解码后每行与原批一致**：内核 `verify_roundtrip`（8 行等具体计数）+
  HTTP 响应 `verification` 块 + `/v1/verify` 从存储重新解码 + 属性测试
  （40 个固定种子的随机运行）。
- **顺序无关声明范围**：`test_order_independence_scope` 与
  `test_http_order_invariance_of_remap` 核验批次序/行序不变性，并显式断言
  「值集合改变则编号可变」这一边界，不做过度承诺。
- **不只检查「接口能调用」**：每个测试断言具体编号/位宽/行数/HTTP 码/错误类别；
  失败测试断言精确 category 与 details。
- **参考答案不由被测核心自己生成**：`tests/oracle.py` 是依据规格另写的朴素
  set/dict 实现，**不 import 任何 `dictsvc.core` 代码**；随机属性测试只用
  oracle 计算期望，再与内核双向比对（oracle 解码 == oracle 从原批直解）。
- **失败可关联输入/运行身份**：每次运行写独立日志
  `data/logs/run-<run_id>.log`（测试用临时目录），开头记录 Python/PyArrow/
  FastAPI/SQLite 版本，随后是每个计算步骤（batch_validated→global_merged→
  width_decided→batch_remapped→roundtrip）与判定依据 `VERDICT ok=... basis=...`；
  pytest 每个用例前打印 `--- TEST nodeid ---`，失败种子会打印 `seed=N`。
  `test_results/logs/session-info.log` 记录整个测试会话的版本。

### 当前测试结果

见 `test_results/`：`pytest.xml`（JUnit，机器可读）、`pytest.out`（完整输出）。
最近一次运行：**全部通过**（数量以该目录中的记录为准）。无跳过/未执行项；
开发过程中失败过的用例（Arrow `.chunks`、`.get_json`、失败运行覆盖成功运行、
矩形表长度）在修复后均已转为通过，git 历史保留了这一过程。
