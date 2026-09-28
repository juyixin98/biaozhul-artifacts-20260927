# 词典最优切分后端 (Optimal Dictionary Segmentation Backend)

基于 **Python 3.10+ / FastAPI / SQLite** 的词典分词服务。输入原文，经规范化后在
**有向无环图（DAG）** 上求**最优切分**（词频负对数概率代价的最短路），并返回
**最优路径、次优路径及差距（runner-up gap）**、每段的**原始偏移**与规范化映射。
词典以**不可变完整版本**发布，请求在开始时固定版本。所有数据均为本地合成夹具，
无需任何生产账号或外部服务。

---

## 1. 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # 或: pip install -r requirements.txt

# 一键本地演示（无需起服务，直接走完整算法+版本链路）
python scripts/demo.py

# 启动 HTTP 服务（首次启动会自动播种合成词典）
uvicorn app.main:app --reload
# 或 python -m app.main
```

服务默认监听 `http://127.0.0.1:8000`。首次启动从
`app/data/seed_dictionary.json`（合成数据）发布 `current` 版本。

### 核心调用

```bash
curl -s -X POST http://127.0.0.1:8000/segment \
  -H 'Content-Type: application/json' \
  -d '{"text":"南京市长江大桥"}'
```

返回（节选）：

```json
{
  "request_id": "…",
  "version": "v-20260927-…",
  "normalized_text": "南京市长江大桥",
  "tokens": [
    {"kind":"dict","surface":"南京市","cost":…,"norm_start":0,"norm_end":3,"orig_start":0,"orig_end":3},
    {"kind":"dict","surface":"长江大桥", …, "orig_start":3,"orig_end":7}
  ],
  "best":      {"surfaces":["南京市","长江大桥"], "cost":…, "token_count":2},
  "runner_up": {"surfaces":["南京","市","长江大桥"], "cost":…, "token_count":3},
  "gap": 7.594…,
  "gap_rounded": 7.594535,
  "gap_status": "available",
  "coverage": {"orig_covered": true, "reconstructed": true,
               "orig_ranges": [[0,3],[3,7]]},
  "diagnostics": { … }
}
```

---

## 2. 模块职责（真实分层，非单文件脚本）

| 路径 | 职责 |
| --- | --- |
| `app/config.py` | 配置（环境变量前缀 `SEG_`），全部带本地默认值 |
| `app/core/normalize.py` | 文本规范化（逐字符 NFKC + casefold + 显式零宽字符删除）与**原文偏移映射** |
| `app/core/cost.py` | 词频 → 负对数概率代价（unigram MLE）；支持显式 `cost` 覆盖 |
| `app/core/trie.py` | 不可变内存 Trie（词典索引） |
| `app/core/dag.py` | DAG 边枚举：词典边 + **确定性单字未知词回退边** |
| `app/core/segmenter.py` | DAG 上 top-2 不同路径的动态规划、稳定同代价裁决、偏移/覆盖校验 |
| `app/storage/models.py` | 发布载荷模型与**带失败类别的整批校验** |
| `app/storage/repository.py` | SQLite 版本仓库（事务原子发布、版本不可变、内容寻址版本号） |
| `app/storage/snapshot.py` | 某一版本的不可变快照（解析后的代价 + Trie） |
| `app/storage/registry.py` | 版本注册表：发布、解析/固定版本、执行切分并写诊断 |
| `app/diagnostics.py` | 请求级结构化诊断（accept / reject / undetermined + 原因 + 关键状态） |
| `app/redaction.py` | 脱敏：默认只输出长度与 SHA-256 指纹，绝不打印原文 |
| `app/logging_setup.py` | 日志，注入 `request_id` |
| `app/api/` | FastAPI 路由 / schemas / 错误信封 / 依赖 |
| `app/main.py` | 应用工厂、lifespan（建库播种）、全局异常处理、可运行入口 |
| `app/data/seed_dictionary.json` | 合成种子词典（夹具） |
| `scripts/demo.py` | 本地端到端演示脚本 |
| `tests/` | 独立组织的测试（金标准/穷举 oracle/版本/规范化/诊断/HTTP） |
| `tests/oracle.py` | **独立参考实现**：全切割枚举 + 独立重算对数代价，不调用生产核心 |

---

## 3. 关键行为语义

### 3.1 规范化与偏移（变长字符）

逐字符：`NFKC(ch).casefold()`，并**显式删除**一组零宽/格式字符
（`SOFT HYPHEN U+00AD`、`ZWSP U+200B`、`ZWNJ U+200C`、`ZWJ U+200D`、
`BOM U+FEFF`、`WORD JOINER U+2060`）。

- 展开：`ﬁ → fi`、`ß → ss`（casefold）；
- 全角/兼容形式映射到标准形式（`Ａ → a`）；
- 每个 token 同时给出规范空间 `[norm_start, norm_end)` 与原文空间
  `[orig_start, orig_end)`；
- 原文区间**严格平铺**：无缺口、无重叠，`original[start:end]` 拼接后与输入
  **逐字符相等**（`coverage.orig_covered` / `coverage.reconstructed`）。被删除
  的零宽字符前向附着到后一个 token；整段均为格式字符时输出一个 `kind="format"`
  的 token 覆盖整段原文。

### 3.2 未知词（OOV）回退：长度与代价确定，不丢字符

- 回退边长度恒为 **1 个规范字符**；
- 代价恒为配置项 `SEG_UNKNOWN_CHAR_COST`（默认 `8.0`）；
- 仅当该单字**本身不是词典词**时才发出回退边（避免与词典边重复）；
- 任意位置总有一条出边直到终点 ⇒ **原文必然被完整覆盖，绝不丢字符**；
- 回退 token 的 `kind` 为 `"unknown"`，响应中以 `unknown_tokens` 计数。

### 3.3 最优与次优；同代价稳定裁决

在边界顶点上做从左到右的 DP，每个顶点保留**两条不同的**最优路径（不同 = token
表面序列不同），因此 runner-up gap 是“另一种切分”的代价差而非重复标签。
路径按以下键做全序比较（全部升序）：

```
(总代价, token 数, 规范化 token 表面序列的字典序)
```

- 代价更低者胜；
- 代价相同 ⇒ **token 数更少者胜**（优先整词）；
- 仍相同 ⇒ **表面序列字典序更小者胜**。

裁决与边的插入顺序、词典构建顺序、平台均无关，结果确定。

实现上有两条容易出错、被回归测试专门锁定的规则：

1. **延迟剪枝**：每个顶点必须先收集全部入边候选，再一次性剪枝；边展开过程中
   不能按“临时第二名”提前丢弃（否则一条在该顶点暂居第三、但在下游反超的路径会被
   漏掉）。见 `tests` 中 `test_g9_tied_interior_prefixes_are_not_pruned`。
2. **浮点并列量化**：两条由相同词代价、不同求和顺序构成的路径，裸浮点累加会有
   ~1e-15 的差异。所有排序/并列/剪枝比较使用 1e-9 量化的代价键（报告值仍为精确
   浮点），使同代价裁决不依赖加法顺序。见
   `test_g10_float_summation_noise_treated_as_tie`。

`gap = cost(次优) − cost(最优)`；当且仅当只存在一条可行切分时
`gap_status = "unique_path"`、`gap = null`（次优差距**无法判定**，诊断中说明）。

### 3.4 版本发布与请求固定

- `POST /admin/dictionaries` 以**整批**方式发布一个新版本：版本行与其全部词条在
  **一个事务**中原子可见；任一词条校验失败则**整批拒绝**，不产生版本。
- 版本不可变；旧版本保留，可继续固定读取。
- 版本号是**内容寻址**的（日期 + 条目内容 SHA-256 前缀）；相同内容重复发布是
  幂等的（重新选中为 current，不新增重复版本）。
- 请求在开始时通过 `X-Dictionary-Version`（或默认 current）解析一次并拿到
  **不可变快照**；请求进行期间即使发布了新版本，也不影响该请求（in-flight pinning）。

---

## 4. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET`  | `/health` | 健康检查，返回 current 版本与词数 |
| `POST` | `/segment` | 切分。可带请求头 `X-Dictionary-Version` 固定版本、`X-Request-Id` 固定请求标识 |
| `POST` | `/admin/dictionaries` | 原子发布新版本，`201` |
| `GET`  | `/admin/dictionaries` | 列出全部版本（含历史、current 标记） |

发布体：

```json
{
  "note": "optional",
  "entries": [
    {"surface": "研究", "frequency": 100},
    {"surface": "南京市", "cost": 5.0}
  ]
}
```

`frequency`（正整数）与显式 `cost`（正数）二选一：给 `cost` 时直接使用，
否则按 `log(F / f)` 由词频推导（`F` 为所有按频率计的词条频次之和）。

### 错误语义（统一信封）

所有错误返回：

```json
{"error": {"code": "<机器可读码>", "message": "<说明>",
           "request_id": "<请求标识>", "details": …}}
```

| HTTP | code | 触发条件 |
| --- | --- | --- |
| `422` | `invalid_request` | 请求体不符合 schema（如缺 `text`）。`details` 仅含字段位置/类型，不回显正文 |
| `400` | `invalid_dictionary` | 发布批校验失败。`details.issues[]` 每条带 `index` 与失败 `code`，整批拒绝、无版本产生 |
| `409` | `version_not_found` | `X-Dictionary-Version` 固定的版本不存在 |
| `500` | `internal_error` | 未预期内部错误（细节仅入日志，带 request_id） |

发布校验失败类别（`issues[].code`）：

`empty_batch`、`not_a_list`、`entry_not_object`、`surface_missing`、
`surface_not_string`、`surface_empty`、`normalized_empty`（规范化后为空，如纯零宽字符）、
`frequency_invalid`、`cost_invalid`、`duplicate_surface`（两个 surface 规范化后同键，
例如 `ﬁ` 与 `fi`）。

### 诊断

每次 `/segment` 的 `diagnostics` 都带 `request_id` 和有序事件，每个事件含
`stage / decision / reason / state / elapsed_ms`：

- `version_resolution`：接受（固定了哪个不可变版本、词数）或拒绝（版本不存在）；
- `normalization`：原文长度、规范长度、删除字符数、偏移是否平铺；
- `dag_search`：token 数、OOV 数、最优代价、gap 与 `gap_status`、覆盖/重建结果。

`decision ∈ {accept, reject, undetermined}`；`gap_status="unique_path"` 即
“次优差距无法判定”的明确记录。**日志与持久诊断默认只记录输入的长度和
SHA-256 指纹**，不打印原文；仅当显式设置 `SEG_LOG_REVEAL_TEXT=true` 才会附带
≤16 字预览（本地调试用）。

---

## 5. 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SEG_DB_PATH` | `app/data/dictionary.db` | SQLite 路径 |
| `SEG_SEED_PATH` | `app/data/seed_dictionary.json` | 种子词典 |
| `SEG_AUTO_SEED` | `true` | 无版本时是否自动播种 |
| `SEG_UNKNOWN_CHAR_COST` | `8.0` | 单字 OOV 回退代价 |
| `SEG_MIN_WORD_COST` | `0.01` | 词代价下限（避免零/负代价） |
| `SEG_MAX_WORD_LENGTH` | `32` | Trie 最长匹配词长 |
| `SEG_LOG_LEVEL` | `INFO` | 日志级别 |
| `SEG_LOG_REVEAL_TEXT` | `false` | 是否在日志中附带原文预览（默认脱敏） |

---

## 6. 测试与复现

```bash
python -m pytest                 # 全部测试
python -m pytest -v              # 查看每条用例名
```

测试不是“接口能调通”式断言，而是**断言具体结果与失败类别**：

- `tests/test_golden_segmentation.py`：**手工给出**的金标准——歧义短句
  `研究生命` 的精确最优/次优路径、代价与 gap；同代价“token 更少优先”和
  “字典序优先”两个独立案例；OOV 回退长度/代价/不丢字符；`ﬁsh`、`aßb`、
  `STRAßE` 变长规范化及其中间空切片；零宽字符（中间、纯格式串）；重复词；空串；
  单字词典词的 unique-path。
- `tests/oracle.py`：**独立参考实现**——对每种切割位置做穷举递归（无 Trie、无 DP、
  不 import 生产核心），并用原始词频独立重算 `log(F/f)`。金标准答案不是由被测核心
  生成；生产结果与 oracle 双向核验。
- `tests/test_exhaustive.py`：30 组**固定随机种子**的小词典，对小句穷举所有切分，
  逐一断言最优表面序列、次优、精确 gap，并断言覆盖连续、可重建。
- `tests/test_versioning.py`：原子发布、旧版本保留、in-flight 固定、内容寻址幂等、
  以及全部发布失败类别（`code` 级别断言）。
- `tests/test_normalize.py`：展开/删除/偏移平铺不变量。
- `tests/test_diagnostics.py`：请求标识、accept/reject/undetermined、脱敏不含原文。
- `tests/test_api.py`：真实 ASGI 应用（FastAPI TestClient）端到端，含 200/201/
  400/409/422 的具体契约。

---

## 7. 项目布局

```
app/
  config.py
  diagnostics.py  redaction.py  logging_setup.py
  core/   normalize.py cost.py trie.py dag.py segmenter.py
  storage/ models.py repository.py snapshot.py registry.py
  api/    schemas.py errors.py deps.py routes.py
  main.py
  data/   seed_dictionary.json
scripts/demo.py
tests/   oracle.py conftest.py test_*.py
pyproject.toml  requirements.txt  README.md
```
