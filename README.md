# 加权编辑距离候选纠错服务

基于**非限制性 Damerau–Levenshtein（Lowrance–Wagner 递推）**的候选纠错后端：
Python + FastAPI + SQLite，全部本地运行，无外部服务依赖。

- **明确变体**：非限制性（true Damerau–Levenshtein，允许对同一子串多次编辑，
  交换链 `CA → ABC` 距离为 2）。**不与限制性最优串对齐（OSA）递推混用**；
  测试里专门实现了一份 OSA 作负对照。
- **加权操作**：insert / delete / substitute / 相邻 swap，代价非负；
  substitute 支持有向字符对优惠表（可非对称）。
- **可重算**：每个候选返回具体编辑路径（`insert/delete/substitute/swap` 的
  具体下标与字符），客户端可以逐步重放并按代价模型独立重算成本；
  服务端在返回前也会自检“重放得到目标串、重算成本 == DP 距离”。
- **可解释**：每个响应带规范化步骤、7 个流水线阶段的计数/耗时、版本号、
  请求身份（`x-request-id`）、失败类别与“阈值边缘不确定”单列。
- **有界**：查询长度、候选展开数、返回数都有上限；展开截断会明确标注
  结果不确定。

---

## 目录结构

```
app/
  config.py          # 配置（JSON + 环境变量 + 默认值）与上限
  costs.py           # 非负代价模型（含校验、有向替换表）
  normalization.py   # NFKC + casefold + 空白规范化（纯函数、记录步骤）
  editdistance.py    # 核心：加权非限制性 DL 递推、回溯、路径编译/重放/重算
  index.py           # 长度分桶索引 + 两个可接纳剪枝下界
  lexicon.py         # SQLite 不可变版本存储
  query.py           # 查询编排（7 阶段诊断、稳定排序、不确定标注）
  service.py         # FastAPI 路由、错误分类、请求身份
  logging_setup.py   # 结构化 JSON 日志
scripts/init_db.py   # 用种子夹具初始化 SQLite 版本
config/              # costs.json / settings.json
data/seed_lexicon.jsonl  # 本地合成词典夹具（50 条）
tests/
  oracle.py          # 独立参考：具体字符串状态空间上的 Dijkstra 最短路
  test_editdistance.py  # 对拍 oracle、OSA 对照、重复字符、交换链、非对称代价
  test_index_bounds.py  # 下界可接纳性穷举对拍、剪枝不漏阈值内候选
  test_normalization.py
  test_query.py      # 阈值边缘、稳定排序、上限、不确定标注
  test_api.py        # 端到端：具体结果、失败类别、请求身份、版本管理
```

---

## 本地启动

需要 Python 3.10+（开发环境为 3.12）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt     # 或 requirements.txt（仅运行）

# 1) 初始化本地 SQLite（data/spellcheck.db），创建并激活 seed-v1
python -m scripts.init_db

# 2) 启动服务
uvicorn app.service:app --host 127.0.0.1 --port 8000
```

锁定文件：`requirements.lock`（由 `pip freeze` 生成，记录完整传递依赖版本）。

### 环境变量

| 变量 | 作用 |
|---|---|
| `SPELLCHECK_SETTINGS` | settings JSON 路径（覆盖 `config/settings.json`） |
| `SPELLCHECK_COSTS` | 代价配置 JSON 路径（覆盖 `config/costs.json`） |
| `SPELLCHECK_DB_PATH` | 覆盖 SQLite 数据库文件路径 |

---

## 示例请求

```bash
# 基本纠错（相邻交换距离 1）
curl -s http://127.0.0.1:8000/v1/correct \
  -H 'Content-Type: application/json' \
  -H 'X-Request-Id: demo-001' \
  -d @examples/correct_basic.json | python -m json.tool

# 非对称/自定义代价：让 swap 更便宜（recieve -> receive 距离 0.6）
curl -s http://127.0.0.1:8000/v1/correct \
  -H 'Content-Type: application/json' \
  -d @examples/correct_asymmetric_costs.json | python -m json.tool

# 大小写/全角规范化
curl -s http://127.0.0.1:8000/v1/correct \
  -H 'Content-Type: application/json' \
  -d @examples/correct_casefold.json | python -m json.tool
```

请求体：

```json
{
  "query": "speling",
  "threshold": 2.0,
  "max_results": 5,
  "costs": {"transpose": 0.6}
}
```

`costs` 为可选项，仅覆盖本次请求（与全局配置合并）；`threshold` 省略表示
不设阈值（按 `max_results` 截断）。

响应（节选）：

```json
{
  "query": "speling",
  "version_id": "seed-v1",
  "candidates": [{
    "word": "spelling",
    "freq": 500,
    "distance": 1.0,
    "lower_bound": 1.0,
    "within_threshold": true,
    "edit_path": [{"type": "insert", "index": 5, "char": "l"}],
    "path_length": 1
  }],
  "uncertain": [],
  "normalization": {"steps": []},
  "diagnostics": {
    "stages": [
      {"name": "normalize", "counts": {"raw_length": 7, "normalized_length": 7}},
      {"name": "generate", "counts": {"candidates_after_length_bound": 12}},
      {"name": "prune", "counts": {"kept": 3, "pruned": 9}},
      {"name": "evaluate", "counts": {"evaluated": 3}},
      {"name": "rank", "counts": {"within_threshold": 1, "returned": 1}}
    ],
    "notes": [],
    "index_size": 50
  },
  "request_id": "demo-001"
}
```

### 编辑路径语义

路径作用在“当前字符串”上（下标随操作动态变化）：

- `{"type":"insert","index":5,"char":"l"}`：在位置 5 插入 `l`；
- `{"type":"delete","index":2,"char":"e"}`：删除位置 2 的 `e`；
- `{"type":"substitute","index":0,"char":"s"}`：位置 0 替换为 `s`；
- `{"type":"swap","index":3,"detail":"eh<->he"}`：交换位置 3、4 的相邻字符。

### 其他端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + 当前激活版本 |
| GET | `/versions` | 列出所有版本（含 checksum、条目数、激活标志） |
| POST | `/versions` | 创建不可变版本（`{entries:[{word,freq}], version_id, activate}`） |
| POST | `/versions/{id}/activate` | 激活版本 |

---

## 运行测试

```bash
source .venv/bin/activate
python -m pytest                 # 全部
python -m pytest tests/test_editdistance.py -k unrestricted
```

测试策略（断言具体结果与失败类别，不是“接口能调用”）：

1. **独立 oracle 对拍**：`tests/oracle.py` 在**具体字符串**状态空间上跑
   Dijkstra（状态是真实字符串、边是四种具体操作），与 DP 表完全独立。
   80 组固定随机种子（单位/加权代价各 40 组）+ 手工重复字符/交换链/
   非对称代价用例逐一对拍；每条路径都重放并独立重算成本。
2. **变体对照**：测试内实现限制性 OSA 递推，断言 `CA→ABC` 上
   本实现 = 2 而 OSA = 3，防止混用递推。
3. **剪枝不漏**：180 组随机串断言两个下界都 ≤ 真实距离（可接纳性），
   并在多个阈值（含 0、0.5、1.0…）上断言阈值内候选无一被剪枝。
4. **阈值边缘**：距离恰好等于阈值的候选必须保留（`<=` 而非 `<`），
   并进入 `uncertain`（默认 0.5 边缘带）。
5. **上限与失败类别**：空串 `empty_query`、超长 `query_too_long`、
   负阈值 422、候选展开截断必须产生 `notes` 不确定说明。

---

## 支持范围与关键取舍

- **递推形式**：在“插入代价均匀、删除代价均匀”的前提下使用 O(nm)
  last-occurrence 递推；它与枚举全部 `(k,l)` 对的完整 Lowrance–Wagner
  O(n²m²) 递推等价——非负代价下更近的 k / l 支配更远的对
  （`d[k1-1][l-1] + (i-k1-1)·w_del ≤ d[k2-1][l-1] + (i-k2-1)·w_del`）。
  **不支持按具体字符变化的 insert/delete 单价**（会破坏该支配关系）；
  substitute 的字符对优惠不影响递推结构，因此支持且可有向、非对称。
- **swap 语义**：仅相邻交换；宏递推里交换的是删除中间字符后变为相邻的
  两个存活字符，中间目标字符作为插入放在二者之间（Lowrance–Wagner 原语义）。
- **长度上限**：规范化后查询默认 ≤ 32 字符（可配置）。这同时界定了
  单次 DP 的 O(nm) 规模；超长请求返回 422 `query_too_long`，不做静默截断。
- **候选上限**：默认最多评估 2000 个候选；按（下界, 词序）确定顺序后截断，
  触发截断时在 `diagnostics.notes` 明确标注“可能漏候选”。
- **排序稳定性**：`(distance, word, -freq)`；距离与词完全确定名次，
  频次只决定完全并列时的展示顺序。平局打破顺序在 DP 内部也固定
  （diag > delete > insert > transpose），路径确定可复现。
- **存储**：词典版本不可变（SQLite 快照 + checksum），同一时刻一个激活
  版本；版本切换后索引缓存按版本重建。单进程部署，未做跨进程缓存同步。
- **规范化只做无外部依赖的确定性变换**（NFKC / casefold / 空白），
  不接入键盘模型、语言模型或真实用户数据。
- 浮点比较使用 1e-9 容差；所有代价在加载时校验为有限非负数。
