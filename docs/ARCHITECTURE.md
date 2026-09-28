# 架构与关键判定依据

## 分层

```
HTTP (api/app.py, api/schemas.py)
  └─ 服务编排 (service.py): 句柄注册表 + 事务边界 + 审计 + 失败分类
       ├─ 格式适配 (adapters.py): pylist / ipc_stream / raw_buffers
       ├─ 执行内核 (kernel/view.py, kernel/concat.py, kernel/bitmap.py)
       ├─ 独立验证 (kernel/checks.py)        ← 不使用被测内核的结论
       └─ 元数据事务 (metadata.py, SQLite WAL)
配置 config.py · 日志 observability.py · 版本 versions.py
```

依赖方向：API → service → {adapters, kernel, metadata}；`checks.py` 只依赖
`bitmap.py`、numpy 和标准库，**不导入 view/concat/adapters**，因此它作为
验证器不会"用被测代码给自己出答案"。

## 零拷贝模型

Arrow 数组 = `[validity][, offsets][data]` + 逻辑 `offset/length`。

- `ColumnView.buffers` 保存 `pyarrow.Buffer` 元组。Buffer 持有底层分配，源
  `pa.Array`/`RecordBatch` 包装对象被回收不影响视图。
- `slice(o, n)` 返回同一 buffer 元组、`offset += o` 的新 `ColumnView`，不分配。
- IPC stream 导入时，PyArrow 的 zero-copy reader 让列 buffer 直接驻留在
  payload 分配内部（实测见 verify 脚本第 6 节打印的 `payload_relative_offset`）。
  视图通过 `_owners=(payload_buf,)` 额外钉住 payload，源 bytes/reader 释放后
  地址与取值保持不变。

NULL 判断永远是 `validity[(offset+i) >> 7] >> ((offset+i) & 7) & 1`，
不调用 `pyarrow` 的标量访问器；位图辅助代码是纯 Python 并有独立单测。

## 验证规则（checks.py）

| 层 | 规则 | 失败码 |
|---|---|---|
| type | 仅支持定宽数值与 utf8；buffer 个数正确（2 或 3） | `TYPE_UNSUPPORTED` / `BUFFER_LAYOUT` |
| validity | 覆盖 `offset+length` 所需字节数 | `BUFFER_TOO_SHORT[validity]` |
| validity | 最后一个字节的尾部 padding 位必须为 0；多余字节必须全 0 | `INVALID_PADDING` |
| validity | 无位图时 null_count 必须为 0；给了 claimed 值时逐位重算比对 | `NULL_COUNT_MISMATCH` |
| offsets | 需要 `(offset+length+1)*4` 字节 | `BUFFER_TOO_SHORT[offsets]` / `MISSING_BUFFER` |
| offsets | 逻辑起点 offset=0 时首偏移必须为 0 | `INVALID_FIRST_OFFSET` |
| offsets | 单调非递减、非负 | `DECREASING_OFFSET` / `OFFSET_OUT_OF_BOUNDS` |
| data（定宽） | `(offset+length)*byte_width ≤ size` | `BUFFER_TOO_SHORT[data]` |
| data（utf8） | 每个偏移 ≤ data 大小；最终偏移 ≤ data 大小 | `OFFSET_OUT_OF_BOUNDS[data]` |
| data（utf8） | 非 NULL 槽位的字节跨度可 UTF-8 解码 | `UTF8_INVALID` |

一次检查返回全部违规（不遇错即停），每条违规带 `code/layer/message/index`。
适配层在导入前调用它；`/api/v1/validate` 直接暴露它；raw 导入还会再让
PyArrow 的 `Array.validate(full=True)` 复核一次（双保险）。

注意：空串 `""` 是有效槽位（`offsets[i] == offsets[i+1]`），与 NULL 的区别
只看 validity，不看跨度。

## 拼接与类型

- 输出稠密数组：validity（需要时）+ data（字符串加 offsets）均为精确大小的
  新分配；字符串数据长度按各 chunk 逻辑窗口 `[lo, hi)` 预算，NULL 占零宽度。
- 同类型直接拼接；类型集合 >1 且未指定 `cast_to` → `CastError`
  （服务层映射为 422 `TYPE_MISMATCH`），绝不隐式转换。
- cast 通过 numpy `astype(casting="safe")` 执行：数值加宽允许；窄化溢出抛
  `CastError("unsafe cast …")`；字符串↔数值不提供转换。

### CopyLedger

对每个输出 buffer 记录 `address/size`，并把其区间与所有源 buffer 的 span
比较：完整落在某个源 span 内 ⇒ `aliased_source=<源地址>, copied=false`；
否则 `copied=true` 并计入 `copied_bytes`。另报告
`allocator_delta_bytes = pa.total_allocated_bytes()` 的前后差，供人工旁证。

## 元数据事务（metadata.py）

SQLite（WAL、外键开）。三张表：`runs / arrays / operations`。

- 正常提交：业务行（注册数组）与审计行在同一个 `BEGIN IMMEDIATE` 事务提交。
- 业务失败（rejected）：业务事务回滚后，审计行以独立事务提交为
  `rejected` + 精确错误类别，保证拒绝也留痕但不留业务脏数据。
- 未预期异常（failed）：同样回滚业务，再独立写 `failed`。`operations.status`
  有 CHECK 约束，写入未知状态会被数据库拒绝。

## 日志与可关联性

`RunLogger` 每行 JSON：`ts / run_id / event(started|completed|rejected|failed)
/ step / verdict / versions{python,pyarrow,arrowzero,platform} / detail`。
测试侧 `tests/helpers.py:TestLog` 产生同样结构的用例日志；HTTP 侧用
`x-run-id` 把多步操作串成一条可在 `GET /api/v1/runs/{id}` 查询的时间线。

## 测试 oracle 的独立性

1. **纯 Python oracle**（`tests/helpers.py`）：直接从原始 validity 字节算
   NULL 图、从 int32 偏移和 data 切片算字符串值，不导入内核。
2. **PyArrow oracle**：`to_arrow().to_pylist()`、`validate(full=True)` 与
   `pa.concat_arrays` 作为第二参考。
3. 随机属性测试：种子确定（可复现），切片起点覆盖字节边界 7/8/15；每个
   切片同时与两个 oracle 比对。
4. 非法用例来自 `samples/fixtures.py` 的显式构造字节（如 `[0,3,2,5]` 递减
   偏移），不是由被测实现序列化生成。
