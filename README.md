# Unicode Text Index Service

基于**固定 Unicode 数据版本**的文本索引服务，在 **字节 / 码点 / 扩展字素簇
（extended grapheme cluster）** 三种位置空间之间做双向映射，并支持把编辑
增量应用到索引。分段使用成熟库（`grapheme`，UAX #29），**双向索引与增量
更新全部自行实现**。

- 组合符、ZWJ 序列、区域指示符（旗帜）一律按 UAX #29 分段，绝不按字符数猜边界
- 非法 UTF-8 严格拒绝（超长编码、错误延续、截断、代理项码点）
- 编辑锚点必须落在合法边界：字节锚点必须是 UTF-8 前导字节且为簇边界
- 每个索引版本通过 SHA-256 **绑定原文摘要**，并记录 Unicode/Blob 数据版本
- 四类失败严格区分：`input_error` / `state_conflict` /
  `resource_exhausted` / `computation_failure`

## 固定的数据版本

| 项 | 值 |
|---|---|
| 分段库 | `grapheme==0.6.0` |
| Unicode 数据库 | **13.0.0**（随库固定） |
| 独立校验预言机 | `regex==2026.9.10` 的 `\X`（仅测试用，服务代码不导入） |
| 索引 Blob 格式 | `blob1:grapheme-0.6.0/unicode-13.0.0` |

数据版本写入每个 Blob 头、文档行、`/version` 响应；版本不一致的旧 Blob
在读取时以 `index_version_mismatch` 拒绝，而不是被静默误读。

> 已知上游缺陷与修正：`grapheme 0.6.0` 的 FSM 在 `Prepend` 状态遇到
> LF/Control 后错误地回到 `default` 状态，可能把后续 ZWJ 错误并入控制符簇。
> `src/textindex/segmenter.py` 依据 UAX #29 **GB3–GB5 的硬性不变量**
> （CR LF 成一簇；其余 CR/LF/Control 必为单例，且优先级高于 GB9b）对库
> 输出做窄幅修正。该修正由 300+ 随机串与独立 `regex \X` 预言机交叉验证。

## 目录结构（模块边界与契约）

```
src/textindex/
  unicode_version.py  固定版本常量（唯一事实来源）
  errors.py           错误分类/错误码/HTTP 状态/结构化 details（跨模块错误契约）
  encoding.py         文本规范①：严格 UTF-8 解码、代理项拒绝、前导字节判定
  normalizer.py       文本规范②：NFC/NFD/NFKC/NFKD/NONE
  segmenter.py        算法索引①：成熟库分段 + GB3–GB5 不变量修正
  index.py            算法索引②：自建双向索引、边界谓词、版本化校验和 Blob
  edits.py            算法索引③：自行实现的增量窗口重分段与偏移拼接
  digest.py           版本存储：原文 SHA-256
  storage.py          版本存储：SQLite 文档表 + append-only versions 表
  service.py          查询/诊断：用例编排、四类错误、结构化日志
  diagnostics.py      运行编号(run_id)与 JSONL 可重放日志
  config.py           环境变量配置
  api.py              FastAPI HTTP 边界与统一错误信封
tests/
  fixtures.py         合成夹具：组合符/旗帜/ZWJ/肤色/CRLF（带手算常量）
  oracle.py           独立预言机（regex \X + UTF-8 手工扫描，不导入被测核心）
  conftest.py         测试运行编号与判定日志
scripts/
  replay.py           回放服务日志/测试判定日志
  request_samples.sh  可复现请求样例
```

## 从干净目录复现

需要 Python ≥ 3.10（验证环境为 Python 3.12.3 / Linux）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 运行全部测试（不需要先启动服务；自动使用临时 DB/日志路径）
PYTHONPATH=src python -m pytest tests/ -q

# 启动服务
mkdir -p data logs
TEXTINDEX_DB=data/textindex.db TEXTINDEX_LOG=logs/service.jsonl \
  uvicorn textindex.api:app --app-dir src --host 127.0.0.1 --port 8000

# 另一终端：请求样例
bash scripts/request_samples.sh
```

## 依赖版本（requirements.txt）

```
fastapi==0.141.1
uvicorn==0.54.0
pydantic==2.13.5
grapheme==0.6.0
pytest==9.1.1
httpx==0.28.1
regex==2026.9.10     # 仅测试：独立 \X 预言机，服务代码不导入
```

## 配置（环境变量，均有默认值）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TEXTINDEX_DB` | `data/textindex.db` | SQLite 路径 |
| `TEXTINDEX_LOG` | `logs/service.jsonl` | 服务 JSONL 日志 |
| `TEXTINDEX_MAX_DOC_BYTES` | `1048576` | 单文档字节上限（超限 → 413） |
| `TEXTINDEX_MAX_CLUSTERS` | `1000000` | 簇数量上限（超限 → 413） |
| `TEXTINDEX_NORMALIZATION` | `NFC` | 默认规范形式 |

## HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET  /version` | 固定的 Unicode/库/Blob 版本与 run_id |
| `GET  /health` | 健康检查 |
| `POST /documents` | JSON `{text, doc_id?, normalization?}` 建文档 |
| `POST /documents/raw` | **原始字节**入站（严格 UTF-8 解码，非法 → 400） |
| `GET  /documents` / `GET /documents/{id}` | 列表 / 详情 |
| `DELETE /documents/{id}` | 删除 |
| `GET  /documents/{id}/clusters` | 每簇文本与三空间起止位置 |
| `POST /documents/{id}/convert` | `{position, from_unit, to_unit, strict}` |
| `POST /documents/{id}/edit` | `{start,end,replacement,unit,base_digest?}` |
| `POST /documents/{id}/validate` | 与完整重建逐表比对 |
| `GET  /documents/{id}/versions` | 摘要绑定的修订历史 |

错误统一信封（无需解析文案即可区分类别）：

```json
{"error": {"category": "input_error", "code": "illegal_byte_boundary",
           "message": "...", "details": {"byte_offset": 2}},
 "run_id": "...", "request_id": "..."}
```

### 四种失败类别

- **input_error (4xx)**：`invalid_utf8`, `unpaired_surrogate`,
  `empty_document`, `position_out_of_range`, `illegal_byte_boundary`,
  `illegal_codepoint_boundary`, `illegal_grapheme_boundary`,
  `edit_range_crossed`, `invalid_unit`, `unsupported_normalization`
- **state_conflict**：`document_not_found`(404)、
  `document_already_exists`(409)、`digest_mismatch`(409，编辑基于过期摘要)、
  `index_version_mismatch`(409)
- **resource_exhausted**：`document_too_large`(413)、`too_many_clusters`(413)、
  `storage_full`(507)
- **computation_failure (5xx)**：`index_corrupt`（校验和/结构失败）、
  `segmenter_error`、`internal_error`

## 增量更新为何正确

编辑不能在任意位置切：旗帜（RIS 两两配对）、CR×LF、ZWJ 连接都依赖相邻
上下文。算法（`edits.py`）：

1. 锚点先经双向索引的严格边界谓词校验（非法字节/非簇码点直接分类拒绝）；
2. 对旧文本与“新文本整体规范化结果”求最长公共前缀/后缀，定位最小编码差异区；
3. 把差异区向两侧扩到**上下文安全窗口**：吞入整个 RIS 运行段、CR/LF、
   Prepend 链、尾部 ZWJ，保证窗口外的簇不会跨越边界被重新配对；
4. 仅对窗口调用分段器重切，前缀/后缀簇原样复用，按字节/码点位移拼接偏移表。

因此增量结果与“在新文本上完整重建”逐数组相等（测试对此做了断言）。

## 测试策略（断言具体结果与失败类别）

- `test_encoding_normalization.py`：非法 UTF-8 各形态、JSON 代理项、边界谓词、NFC
- `test_segmentation_index.py`：8 个手算夹具（含旗帜/ZWJ/肤色/CRLF）与
  非法边界的**具体错误码**
- `test_property_oracle.py`：300 个随机棘手 Unicode 串，逐表比对独立
  `regex \X` 预言机（参考答案不由被测核心生成）
- `test_edits_incremental.py`：旗帜奇偶、ZWJ 合并、CRLF、三种锚点；
  150 随机 NONE + 80 随机 NFC 编辑，断言**增量 == 完整重建 == 预言机**
- `test_storage_versions.py`：Blob 校验和位翻转/截断/魔数/版本身份、
  摘要冲突、修订历史、跨连接持久化
- `test_api.py`：具体状态码、错误类别/错误码、run_id/request_id 回显

此外在开发期额外跑过 **3000 例三锚点增量模糊** 与 **1000 例 NFC 模糊**，
结果全部与完整重建一致（脚本逻辑见属性测试，可按需扩大参数）。

### 日志与回放

- 服务日志：`$TEXTINDEX_LOG`，每操作一行，含 `run_id / seq / op /
  outcome / code / intermediate(窗口/位移/判定依据) / reason`；
- 测试判定日志：`tests/_run_artifacts/<run>.tests.jsonl`，含运行编号、
  期望/实际、关键中间状态与判定理由。

```bash
PYTHONPATH=src python -m scripts.replay logs/service.jsonl
PYTHONPATH=src python -m scripts.replay tests/_run_artifacts/<run>.tests.jsonl --summary
```

退出码为不一致记录条数（0 表示干净重放）。
