# textindex — 固定 Unicode 版本的文本索引服务

在 **字节（UTF-8）/ 码点（code point）/ 扩展字素簇（extended grapheme cluster）**
三种位置之间建立双向索引，支持按合法边界做增量编辑，且索引版本绑定原文摘要。
技术栈：Python 3.12 + FastAPI + SQLite（stdlib `sqlite3`）。

## 1. 固定的 Unicode 数据版本（核心契约）

| 数据 | 来源 | 钉住版本 |
|---|---|---|
| 扩展字素簇边界（GCB 属性表/UAX #29） | `grapheme==0.6.0` | **13.0.0** |
| 码点类别/规范化等 | Python 3.12 内置 `unicodedata` | **15.0.0** |

- 每个版本行都记录构建时的 `gcb_table_version` / `unidata_version`；
  加载时若与运行环境不一致，返回 `state_conflict / UNICODE_VERSION_MISMATCH`。
- 分段用成熟库 **grapheme**，但**双向索引、边界判定、增量编辑更新全部自行实现**
  （`app/indexing.py`），不按字符数猜边界。
- 测试预言使用**另一个独立第三方库** `regex` 的 `\X`（与 grapheme 不同源），
  再加上手工 UTF-8 长度计算，参考答案不由被测核心自己生成。

## 2. 工程边界

| 模块 | 职责 |
|---|---|
| `app/textnorm.py` | 自实现严格 UTF-8 合法性（D92/D99，区分截断/非法前导/非法延续/过长/代理/超范围，定位偏移）、SHA-256、统计、NFC 诊断 |
| `app/indexing.py` | 双向索引、三空间位置换算、合法边界判定、增量编辑与重同步窗口 |
| `app/storage.py` | SQLite schema、版本/原文原子写入、LEB128+delta 数组编码、操作日志 |
| `app/diagnostics.py` | 存储索引解码、摘要绑定三重校验、逐簇还原（含 GCB 属性） |
| `app/service.py` | 编排：配额、建索引、增量+强制全量重建对照、乐观锁、失败分类与日志 |
| `app/api.py` / `app/main.py` | FastAPI 路由、统一错误信封、run_id 中间件 |
| `app/errors.py` | 四类错误契约（见下） |
| `app/config.py` | 版本常量与配额（环境变量可调） |

## 3. 错误分类（可区分、可断言）

| category | HTTP | 典型 code |
|---|---|---|
| `input_error` | 400/422 | `INVALID_BASE64`、`INVALID_UTF8`（含 reason/offset）、`NOT_A_BOUNDARY`、`POSITION_OUT_OF_RANGE`、`VALIDATION_ERROR` |
| `state_conflict` | 404/409 | `DOCUMENT_NOT_FOUND`、`VERSION_NOT_FOUND`、`VERSION_CONFLICT`、`DOCUMENT_CONFLICT`、`INDEX_CORRUPTION`、`UNICODE_VERSION_MISMATCH` |
| `resource_exhausted` | 413 | `LIMIT_EXCEEDED`（bytes / codepoints / clusters / documents） |
| `computation_failure` | 500 | `INDEX_INCONSISTENT`（增量结果与完整重建不符）、`SEGMENTATION_FAILED` |

编辑契约：`start`/`end` 在任何位置空间给出都必须**同时是字节边界与字素簇边界**。
落在多字节 UTF-8 内部 → `NOT_A_BOUNDARY`（返回最近两个字节边界）；
落在组合符/ZWJ 序列/旗帜内部 → `NOT_A_BOUNDARY`（返回所属簇的起止边界）。

## 4. 从干净目录复现

```bash
# 1) 建虚拟环境并装依赖（版本已锁定）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # 或 -r requirements.lock

# 2) 跑测试（71 个用例）
.venv/bin/python -m pytest -q

# 3) 起服务
TEXTIDX_DB=data/textindex.db .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4) 另开终端，跑可重放请求样例
.venv/bin/python examples/requests_demo.py
# 交互式文档：http://127.0.0.1:8000/docs
```

配置（环境变量，均有默认值）：`TEXTIDX_DB`、`TEXTIDX_LOG_LEVEL`、
`TEXTIDX_MAX_BYTES`、`TEXTIDX_MAX_CODEPOINTS`、`TEXTIDX_MAX_CLUSTERS`、
`TEXTIDX_MAX_DOCUMENTS`、`TEXTIDX_VERIFY_INCREMENTAL`。

## 5. HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 固定/运行时 Unicode 版本与配额 |
| `POST /documents` | body `{doc_id, content_base64}`；非法 UTF-8 拒绝 |
| `POST /documents/{id}/edits` | `{expected_version,start,end,space,replacement_base64}`；乐观锁 |
| `GET /documents/{id}?version=` | 版本元信息、长度、统计 |
| `GET /documents/{id}/convert?position=&from_space=&to_space=&version=` | 位置转换 |
| `GET /documents/{id}/versions` | 不可变版本链 |
| `GET /documents/{id}/clusters?version=&limit=` | 逐簇诊断（码点、GCB 属性、字节范围） |
| `GET /diagnostics/operations` | 最近操作 |
| `GET /diagnostics/runs/{run_id}` | 按 run_id 回放关键中间状态/失败阶段 |

每个响应都带 `X-Run-ID`（可在请求头自带，也可服务端生成）。

### curl 速览

```bash
B=http://127.0.0.1:8000
P=$(python3 -c "import base64;print(base64.b64encode('aé🇺🇳👨‍👩‍👧\r\nb'.encode()).decode())")
curl -s -X POST $B/documents -H 'Content-Type: application/json' \
     -d "{\"doc_id\":\"demo\",\"content_base64\":\"$P\"}"
curl -s "$B/documents/demo/convert?position=15&from_space=byte&to_space=cluster"
```

## 6. 关键设计说明

- **双向索引**：保存 `cp_to_byte`（码点→字节）、`cp_to_cluster`（码点→簇）、
  `cluster_to_cp`（簇→码点）三个单调数组；`cluster_to_byte` 派生。
  存储用 LEB128 + delta 压缩，可独立解码审计（见 `app/storage.py`）。
- **增量编辑**：字节/码点数组直接拼接；簇数组只重算一个**重同步窗口**——
  从编辑点向两侧走到最近的 CR/LF/Control 硬断行边界，并把跨接缝的 CRLF
  整对纳入窗口；窗口内用与全量构建相同的分段器重切。组合符附着、ZWJ 序列、
  旗帜 RI 奇偶、CRLF 配对均由 GCB 状态机在窗口内处理。
- **强制对照**：每次增量编辑后立刻对结果文本完整重建并逐数组比对，
  不一致即 `INDEX_INCONSISTENT`，不落任何版本（60000 次属性编辑 + 24000 次
  连续编辑的随机对照见 `scripts/fuzz_incremental.py`）。
- **版本绑定摘要**：版本行存原文 SHA-256；加载时复算摘要、校验 Unicode 版本、
  再把存储数组与原文重算结果逐数组比对，三重通过才可用。

## 7. 诊断日志

- 服务端 stderr 输出 JSON 行：`ts/level/run_id/path/event/data`；
- 每次操作（成功与失败）写 `operation_log`：阶段（decode → build →
  incremental → full_rebuild_verify → done）、编辑换算后的码点区间、
  替换码点数、错误类别/错误码/错误详情。凭 `X-Run-ID` 即可回放。

## 8. 目录

```
app/            生产代码（见第 2 节模块边界）
tests/          71 个测试：非法UTF-8类别、写死索引值、独立预言对照、
                合法边界往返、增量vs重建、四类错误、摘要绑定、HTTP、run回放
scripts/        增量更新属性模糊脚本（独立于 pytest，可重放 seed）
examples/       HTTP 请求样例
requirements.txt / requirements.lock
```
