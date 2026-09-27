# Search DSL — 规范查询树、版本化存储与检索

一个真实可运行的搜索 DSL：支持字段限定、括号、`AND`/`OR`/`NOT`、隐式连接词、
短语与转义；输出**规范化查询树（canonical query tree）**，在版本化的
SQLite 倒排索引上检索。技术栈 Python 3.11+ / FastAPI / SQLite，全部数据
为本地合成夹具，无外部账号依赖。

## 1. 语言规范（文本规范）

| 构造 | 写法 | 说明 |
|---|---|---|
| 词项 | `fox`、`café`、`价格`、`a-b_c.1` | Unicode 字母/数字；NFKC+casefold 分析，CJK 单字切分 |
| 字段限定 | `title:fox`、`year:[2000 TO 2010]` | 冒号必须紧跟字段名（`title :x` 是语法错误） |
| 短语 | `"quick brown"`、`body:"a AND b"` | 双引号内的 `AND/OR/NOT/()/:` 全部是字面量，绝不是运算符 |
| 转义 | `"say \"hi\""`、`"a\\b"`、`"a\tb"` | 支持 `\"` `\\` `\n` `\t`；悬空反斜杠报 `UNTERMINATED_ESCAPE` |
| 括号 | `(a OR b) AND c`；`()` | 覆盖优先级；空括号 = 空查询语义（match_all） |
| 区间 | `year:[2000 TO 2010]` `{2000 TO 2010}` `[* TO 1999]` | `[ ]` 闭区间，`{ }` 开区间；`*` 为开放边界 |
| 与 | `AND`（大写）或**隐式**：`a b`、`a (b)`、`) (` | 隐式连接词等价 `AND` |
| 或 | `OR`（大写） | |
| 非 | `NOT`（大写） | 优先级最高 |
| 字面 and/or/not | `and`、`Or`、`"AND"` | 小写不是关键字；引号内更不是 |

**优先级（低→高）**：`OR` < `AND`（显式与隐式） < `NOT` < 原子。全部左结合。

```
a AND b OR c      ==  (a AND b) OR c
a b OR c d        ==  (a AND b) OR (c AND d)
NOT a b           ==  (NOT a) AND b
NOT (a OR b)      保持 NOT 节点，不做德摩根展开（防止子句膨胀）
```

字段类型与可用操作：

| 类型 | 词项 | 短语 | 区间 | 备注 |
|---|---|---|---|---|
| `text` | ✅ 分析后匹配 | ✅ 位置连续 | ❌ | 大小写不敏感 |
| `keyword` | ✅ 精确、大小写敏感 | ❌ | ❌ | 可多值 |
| `int` | ✅ 精确 | ❌ | ✅ | |
| `date` | ✅ ISO `YYYY-MM-DD` | ❌ | ✅ | 按规范化日期比较，拒绝 `2021-02-29` |

无字段词项/短语只在 schema 的 `default_fields`（本夹具为 `title`,`body`；
`notes` **不**参与）上做 OR。

### 规范化（canonical）形式

恒等布尔重写，迭代到不动点（因此**幂等**）：拍平、常量吸收
（AND⊥→⊥、OR⊤→⊤、去单位元）、去重、互补对（`a AND NOT a→⊥`、
`a OR NOT a→⊤`）、双重否定、吸收律、单子句提升、子节点全序排序。
**不做 NOT 德摩根分配**（会指数膨胀，违反复杂度预算）。规范 JSON 键排序、
`SHA-256` 作为查询身份哈希。

**关键顺序保证**：字段白名单/类型/复杂度预算在**化简之前**对原始解析树执行
（见 `searchdsl/validate.py`），所以化简永远不可能掩盖“不存在字段”错误；
空查询（match_all）是不动点。

### 复杂度预算（执行前）

`max_query_bytes` 4096、`max_nesting_depth` 12、`max_clauses` 64、
`max_query_terms` 256、`max_phrase_terms` 16、`max_result_window` 1000
（均可在 `config.json` 覆盖）。超限分别返回 `BUDGET_DEPTH/BUDGET_CLAUSES/
BUDGET_QUERY_TERMS/BUDGET_PHRASE_TERMS`。

### 错误类别（稳定 code）

`QUERY_EMPTY`、`QUERY_TOO_LONG`、`UNTERMINATED_STRING`、`UNTERMINATED_ESCAPE`、
`UNBALANCED_PAREN`、`UNEXPECTED_TOKEN`、`RANGE_MALFORMED`、`RANGE_EMPTY`、
`FIELD_UNKNOWN`、`FIELD_TYPE_MISMATCH`、`VALUE_MALFORMED`、
`BUDGET_DEPTH/BUDGET_CLAUSES/BUDGET_QUERY_TERMS/BUDGET_PHRASE_TERMS/BUDGET_RESULT_WINDOW`。
每个错误都带 `[start,end)` 字符位置（见 `examples/ambiguous-examples.json`）。

## 2. 模块划分（真实模块，非单文件/桩）

```
searchdsl/
  spec.py        字段白名单与类型规范（文本规范）
  errors.py      错误分类法（稳定 code）
  astnodes.py    查询树节点 + 规范 JSON/哈希
  lexer.py       词法（短语与转义先于运算符识别）
  parser.py      递归下降：优先级 + 隐式 AND + 区间
  normalize.py   恒等化简到不动点（不做德摩根展开）
  validate.py    执行前白名单/类型/预算（在化简前）
  analysis.py    规范化分词、int/date 解析（算法索引共用）
  store.py       版本化 SQLite + 倒排/精确/标量索引 + 已存查询
  executor.py    规范树集合求值 + explain 判定步骤
  diagnostics.py 关联 run_id/版本/进度/判定的结构化诊断
  config.py      独立配置（默认值 + JSON 覆盖）
  search.py      编排 parse→validate→normalize→store→execute
  service.py     FastAPI
  cli.py         reindex/parse/search/diagnose/serve
tests/           pytest（含独立 oracle 与 300 随机差分用例）
fixtures/        schema.json + 12 篇合成语料 corpus.jsonl
examples/        歧义样例 AST、服务调用示例
scripts/         歧义工件生成、服务演示、全量测试记录
```

## 3. 快速开始与复现

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # 已解析并核验的锁定文件

python -m searchdsl.cli reindex --config config.json   # 建索引（语料变了自动重建）
python -m searchdsl.cli parse  'quick AND (fox OR salmon)' --config config.json
python -m searchdsl.cli search 'title:fox AND NOT tags:dog' --config config.json --explain
python -m searchdsl.cli serve   --config config.json    # http://127.0.0.1:8000/docs
```

一键复现（保留可复核结果）：

```bash
bash scripts/run_tests.sh           # 全量测试 + runs/test-results/<run_id>/ 记录
bash scripts/run_service_demo.sh    # 起真实 HTTP 服务，跑正常(200)+异常(400)
python scripts/ambiguous_examples.py # 重新生成歧义样例 AST/错误位置工件
```

服务调用示例见 `examples/service-calls.md`。

## 4. 测试策略（不只断言“接口能调用”）

* **词法/语法**：精确 token 与字符偏移；歧义样例逐字断言 AST（见
  `tests/test_parser.py` 与 `examples/ambiguous-examples.json`）。
* **化简**：每条布尔恒等式、空查询与不存在字段语义、幂等、
  不做德摩根展开。
* **校验**：每个错误类别都用稳定 `code` 断言；预算逐维度量。
* **具体命中集合**：`tests/test_execution_expected.py` 对 12 篇夹具逐查询
  断言命中文档 id（人工据夹具核对）。
* **随机真值差分**：`tests/test_truth_random.py` 用固定种子生成 300 个
  小型随机表达式，与**独立参考实现** `tests/oracle.py` 对照。oracle
  **不导入**词法/分析/执行/化简，直接对原始夹具文档做朴素 NFKC+正则分词与
  逐文档布尔判定；三方一致（oracle 原树 / 执行器规范树 / oracle 规范树），
  并核验规范化幂等与重渲染哈希稳定。逐用例判定写入
  `runs/truth-differential.jsonl`。
* **HTTP**：FastAPI TestClient 断言 200/400 契约、稳定 code 与位置、
  哈希复用、诊断阶段齐全。
* **诊断**：异常绝不返回成功（`status=error` + `error.code`），日志含
  `run_id`、版本（package/dsl/index/corpus/schema）、阶段进度与判定依据。

## 5. 版本存储

SQLite `meta` 表保存 `package_version`、`dsl_version(dsl-1.0)`、
`index_schema_version(index-1.0)`、`corpus_version`（语料 sha256）、
`schema_version`（schema sha256）、`doc_count`、`built_at`。
打开时若语料/schema 哈希变化则自动重建，杜绝“旧索引配新语料”。
规范化查询以哈希为主键存入 `saved_queries`（含当时的各版本号），
`GET /searches/{hash}` 可取回。

## 6. 目录中的可复核产物

* `runs/test-results/<run_id>/`：`context.json`、`pytest.log`、
  `summary.json`、`truth-differential.jsonl`
* `runs/service-demo/`：真实服务正常/异常响应与 `server.log`
* `runs/diagnose/*.jsonl`：正常与异常查询的逐步诊断
* `examples/ambiguous-examples.json`：歧义样例的明确 AST 与错误位置
