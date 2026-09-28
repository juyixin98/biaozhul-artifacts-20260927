# 词典最优切分后端 (Optimal Lexicon Segmentation Backend)

一个从零实现的中文/文本切分后端：把输入文本视为有向无环图（DAG），用词频代价
求**最优切分**，同时计算**次优路径与差距**，返回每段的原始偏移和规范化映射，
并以**不可变完整版本**发布词典。技术栈 **Python 3.12 + FastAPI + SQLite**，
所有数据均为本地合成夹具，无任何生产账号或真实业务数据。

---

## 1. 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 运行测试（实际执行，见下文“测试”）
python -m pytest -q

# 本地端到端演示（不启动网络服务，进程内跑真实 FastAPI 栈）
python scripts/demo.py

# 启动 HTTP 服务（首次启动自动把 data/seed_lexicon.json 发布为版本 1）
python -m app.main
# 或：uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后：

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/segment \
  -H 'Content-Type: application/json' \
  -d '{"text":"研究生命"}' | python -m json.tool
```

---

## 2. 模块与真实职责

不是单文件脚本，也不是空接口工程，每个模块有独立职责：

| 模块 | 职责 |
|---|---|
| `app/config.py` | 环境变量配置（数据库路径、未知字代价、接近差距阈值、长度上限），不可变 `Settings` |
| `app/normalizer.py` | 文本规范化 + **逐字符映射**：全角 ASCII 1→1、`ß→ss` 1→2、软连字符/零宽字符 1→0；提供脱敏函数 |
| `app/lexicon.py` | 词典领域模型：Trie 前缀索引、词频代价、不可变版本构建、去重、校验和 |
| `app/algorithm.py` | **DAG k=2 最短路**核心：最优/次优路径、稳定平局规则、未知字回退、原始/规范偏移 |
| `app/store.py` | SQLite 版本存储：**整版本原子发布**、冻结版本读取、WAL、写锁 |
| `app/diagnostics.py` | 请求标识 + 决策记录环：接受/拒绝/无法判定原因、关键状态、敏感数据脱敏 |
| `app/service.py` | 用例编排：版本固定（pin）、输入校验、诊断落记录 |
| `app/schemas.py` | Pydantic 请求/响应模型、覆盖连续性校验 |
| `app/api.py` | FastAPI 路由、请求 ID 中间件、统一错误处理 |
| `app/main.py` | 可运行入口 |
| `tests/` | 独立组织的测试，含**独立穷举 oracle** |
| `data/seed_lexicon.json` | 合成词典夹具（词频均为人为构造） |
| `scripts/demo.py` | 本地演示脚本 |

---

## 3. 算法与代价模型

把规范化后的文本建成 DAG：

* 顶点 `0..n` 为字符位置；
* 边 `i → j` 表示把 `text[i:j]` 作为一个词输出；
  * **词典边**：Trie 命中的词，代价为词频代价
    `cost(w) = ln((total_freq + 1) / (freq(w) + 1))`（词越常见越便宜）；
  * **未知字回退边**：每个位置恒有一条长度恰好为 1 的边，代价
    `unknown_char_cost`（默认 **20.0/字**）。因此 `0→1→…→n` 永远是一条
    合法路径，**任何字符都不会被丢弃**；
* 用 DAG 上的 k=2 最短路动态规划，求最优路径与**次优的不同路径**，
  返回 `best_cost`、`second_best_cost`、`cost_gap`。

### 决策语义

| `gap_class` | 条件 | `decision` |
|---|---|---|
| `no_alternative` | 只有一条有类型路径（纯未知串） | `accepted` |
| `close` | `0 < gap < close_gap_threshold`（默认 1.0） | **`indeterminate`（无法判定）** |
| `clear` | `gap ≥ 阈值` | `accepted` |

精确同代价（`gap == 0`）时 `tie_broken=true`，按**完全基于内容**的稳定规则选路，
与词典插入/迭代顺序无关：

1. 代价低者优先；
2. 表面字符串元组字典序（如 `"a","bcd"` 先于 `"ab","cd"`）；
3. 首个分歧位置优先词典词；
4. 词数更少者优先。

### 未知词回退

相邻的单字未知边会在响应中合并为一个 `type="unknown"` 的段，其
**长度（字数）与代价（字数 × 20）显式给出**。例如 `星巴克咖啡`（词典只有
`咖啡`）→ `unknown(星巴克, cost=60)` + `dict(咖啡)`，原始覆盖 `[0,3)+[3,5)`
连续完整。

### 规范化与偏移

响应同时给出：

* `normalized_text` 与 `char_map`：第 k 个规范字符来自哪个**原始下标**
  （1→2 展开时两个规范字符指向同一原始下标；1→0 删除记录在
  `deleted_raw_indices`）；
* 每段的 `norm_start/norm_end`（规范偏移）与 `raw_start/raw_end/raw_text`
  （原始偏移与原始切片）。删除字符（如软连字符）被吸收进相邻段，保证
  `raw_start` 从 0 开始、`raw_end` 到原文长度结束、段间首尾相接；
* `coverage`：`contiguous/complete/raw_length` 可直接核验“原文覆盖连续”。

### 复杂度与性能

* 建图与代价 DP 为 `O(n·L)`，`L` 为最长词（trie 最大深度），空间 `O(n)`；
* 每个顶点只保留最优的 2 条候选。代价选择是线性的；**仅在精确同代价的候选之间**
  才按完整词序列做稳定平局比较（候选数受 trie 深度上界约束）。因此典型文本为
  线性，极端“处处同代价”的人工文本平局比较更重；
* 实测（种子中文词典）：400 字毫秒级，2000 字约百毫秒，4000 字约 0.4 秒。
  HTTP 服务默认上限 2000 字以保证交互延迟；长文档的标准做法是先按句切分，
  需要时可用 `SEGBACK_MAX_INPUT_CHARS` 上调（库层面可处理更长文本，见
  `tests/test_performance.py`）。

---

## 4. 版本发布与版本固定

* `POST /versions` 接收的是**完整词典**（不是增量 diff），先在内存中完成
  规范化/去重/校验和构建，**再在单个 SQLite 事务里**写入 `versions` 与
  `entries`；构建失败或事务失败都不会留下半个版本（原子）。
* 版本一旦发布即冻结，永不原地修改。
* 请求可传 `"version_id": N` 固定版本；即使之后发布了 N+1、N+2，固定版本的
  请求仍返回该版本的结果（测试 `test_pinned_request_stays_on_old_version_after_new_publish`
  覆盖了发布后续版本后旧固定结果不变）。不传则使用最新版本，响应中
  `pinned=false`。

---

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务与最新版本状态 |
| POST | `/segment` | 切分，body：`{"text": "...", "version_id": 1?}` |
| POST | `/versions` | 发布完整新版本，body：`{"words":[{"word","freq"}], "note":""}` |
| GET | `/versions` | 列出全部版本 |
| GET | `/diagnostics?limit=&request_id=` | 最近决策记录或单条记录 |

每个响应都带 `X-Request-ID` 响应头；也可用 `X-Request-ID` 请求头传入自己的
追踪 ID。

### 错误语义（机器可读 `error` 类别）

| HTTP | `error` | 含义 |
|---|---|---|
| 400 | `EMPTY_TEXT` | 文本为空 |
| 413 | `TEXT_TOO_LONG` | 超过 `max_input_chars`（默认 **2000**，建议长文先分句；可用环境变量上调） |
| 404 | `VERSION_NOT_FOUND` | 固定的 `version_id` 不存在 |
| 409 | `EMPTY_LEXICON` | 尚未发布任何词典版本 |
| 422 | `INVALID_PAYLOAD` | 请求体字段缺失/类型错/词频为负/版本号 < 1 |
| 400 | `INVALID_PAYLOAD` | 通过 schema 但构建版本失败（如词过长） |

错误响应统一为：

```json
{ "request_id": "…", "error": "EMPTY_TEXT", "message": "text must be non-empty" }
```

### 诊断与脱敏

每条记录含 `request_id`、`outcome`（accepted/indeterminate/rejected）、
`reason`、`version_id`、字数、最优/次优代价、差距、词数、未知字数等关键状态，
说明为什么接受、拒绝或无法判定。输入正文**只**以脱敏预览保存
（默认保留前 2 个字符 + 长度 + SHA8 指纹），敏感串不会进入诊断记录或日志。

---

## 6. 测试（实际执行）

```bash
python -m pytest -q
# 70 passed
```

测试独立组织在 `tests/`，断言**具体结果与失败类别**，不是“接口能调通”：

* `tests/oracle.py`：**独立于被测核心**的穷举 oracle，用朴素递归枚举 DAG
  的**每一条**完整路径并自行按文档规则求和/排序；与生产代码共享的只有词表
  （测试数据）和代价公式，不共享 DP 实现。
* `test_algorithm.py`：
  * 对有歧义短句 `研究生命`、`北京大学`、`目的的确`、重复词 `哈哈哈哈`、
    未登录词 `星巴克` 等，用 oracle **穷举所有切分**核验最优/次优值；
  * 钉死 `研究生命` 恰有 **12 条有类型路径、5 种表面序列**，并逐一列出；
  * 未知词回退长度=字数、代价=字数×20、不丢字符；
  * 精确平局的稳定选路、且与插入顺序无关；接近差距 → `indeterminate`；
  * 全角 1→1、`ß→ss` 1→2、软连字符删除后偏移连续（含前导/尾随删除字符）。
* `test_store.py`：版本单调递增、旧版本冻结独立、无效负载原子失败、
  规范化去重、校验和。
* `test_api.py`：全部错误类别（400/404/409/413/422）、版本固定跨发布不变、
  原始覆盖连续、诊断脱敏（断言完整敏感串不出现）。

---

## 7. 配置（环境变量，前缀 `SEGBACK_`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEGBACK_DB_PATH` | `data/lexicon.db` | SQLite 文件 |
| `SEGBACK_UNKNOWN_CHAR_COST` | `20.0` | 未知字每字代价 |
| `SEGBACK_CLOSE_GAP_THRESHOLD` | `1.0` | 最优/次优差距小于它则 `indeterminate` |
| `SEGBACK_MAX_INPUT_CHARS` | `2000` | 输入长度上限（长文建议先按句切分；批处理可上调） |
| `SEGBACK_SEED_ON_START` | `1` | 启动时若库为空则发布种子夹具 |

---

## 8. 复现步骤小结

```bash
source .venv/bin/activate
python -m pytest -v          # 穷举 oracle + 具体断言全部通过
python scripts/demo.py       # 歧义/未登录词/重复词/变长规范化/版本固定/诊断脱敏
python -m app.main           # 起服务，再用上方 curl 复现单请求
```
