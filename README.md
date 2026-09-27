# searchdsl：搜索 DSL（字段限定 / 括号 / AND-OR-NOT / 短语 / 转义 → 规范查询树）

Python 3.12 + FastAPI + SQLite 的最小完整实现：把搜索字符串解析为 AST，
执行前完成字段白名单/类型校验与复杂度预算检查，逻辑化简为幂等的规范查询树，
通过 SQLite 倒排索引执行，并对规范树做内容寻址的版本存储。

- DSL 文本规范：[`docs/spec.md`](docs/spec.md)
- 歧义样例 AST 与错误位置：[`docs/ambiguity.md`](docs/ambiguity.md)

## 验收规则对照

| 规则 | 落实位置 |
|---|---|
| 明确优先级与隐式连接词，不把引号内符号当运算符 | `searchdsl/lexer.py`、`searchdsl/parser.py`，测试 `tests/test_parser.py`、`test_lexer.py` |
| 字段白名单与类型检查在执行前完成 | 引擎顺序 parse→validate→budget→normalize→execute：`searchdsl/engine.py`、`searchdsl/schema.py` |
| 逻辑化简保持空查询及不存在字段语义 | `searchdsl/normalize.py`（Empty 恒等）、`schema.py` 先于化简拦截未知字段；测试 `test_normalize.py`、`test_validate_budget.py` |
| 复杂度预算限制深嵌套和子句膨胀 | `searchdsl/budget.py`（默认深 8 / 64 子句，化简前检查）；测试 `test_validate_budget.py` |

## 模块划分（非单文件）

```
searchdsl/
  lexer.py       # 文本规范实现（一）：词法，转义/引号/位置
  parser.py      # 文本规范实现（二）：递归下降语法，优先级与隐式 AND
  ast_nodes.py   # 规范查询树节点 + 确定性序列化
  normalize.py   # 逻辑化简：拍平/去重/排序/双否，幂等
  schema.py      # 字段白名单与 int/text 类型校验（执行前）
  budget.py      # 算法索引（一）：深度/子句复杂度预算
  index.py       # 算法索引（二）：SQLite 倒排表（带位置，短语连续匹配）
  store.py       # 版本存储：文档版本 + 规范树内容寻址 query_versions
  evaluator.py   # 独立参考求值器（纯 Python，第二套答案，不用索引）
  engine.py      # 查询与诊断（一）：阶段流水线
  diagnostics.py # 查询与诊断（二）：JSONL 结构化诊断（run_id 关联）
  config.py      # 独立配置加载
config/searchdsl.yaml   # 字段白名单、类型、预算、路径
service/main.py # FastAPI：/query /documents /queries/{hash} /health
fixtures/documents.json # 10 篇合成夹具
scripts/init_db.py      # 从夹具重建库与索引
examples/               # curl 与 Python 调用示例
tests/                  # 6 个测试文件，102 个用例，断言具体结果与失败类别
docs/                   # 文本规范 + 歧义样例
```

## 快速复现

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt

# 1) 全部测试（102 passed；随机真值对照固定种子 20260928，240 个表达式）
TEST_RUN_TAG=$(date -u +%Y%m%dT%H%M%SZ) .venv/bin/python -m pytest

# 2) 建库（data/search.db：10 篇夹具 + 倒排表）
.venv/bin/python scripts/init_db.py

# 3) 起服务
.venv/bin/uvicorn service.main:app --host 127.0.0.1 --port 8000

# 4) 调用（另开终端；见 examples/calls.md）
curl -s -X POST http://127.0.0.1:8000/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"apple AND (pie OR salad)"}'
SEARCHDSL_URL=http://127.0.0.1:8000 .venv/bin/python examples/client.py
```

## 依赖锁定

- `requirements.txt`：直接依赖（带版本）
- `requirements.lock.txt`：完整传递依赖 `pip freeze`（复现用）

## 测试如何保证“不是接口能调通就算过”

1. **具体结果断言**：25 个手写查询的命中文档集合是逐篇数出来的硬编码期望
   （`tests/test_truth.py::HAND_CASES`），不是被测代码生成的。
2. **答案三方独立**：手工期望、`ReferenceEvaluator`（独立手写分词器 + 内存布尔求值）、
   SQLite 倒排索引执行，三者必须两两一致；规范化前后真值必须一致。
3. **随机性质测试**：固定种子生成 240 个小表达式，每个表达式的输入、
   三套真值、幂等判定逐条落盘到 `logs/runs/<tag>/truth-cases.jsonl`。
4. **失败类别断言**：词法/语法/未知字段/类型/预算分别断言 400/422/413、
   `error.category` 和列位置（见 `docs/ambiguity.md` E1–E13）。
5. **幂等**：`normalize(normalize(x)) == normalize(x)` 在参数化样例与全部随机表达式上断言；
   等价书写（`a AND b` / `b AND a` / `a b`）共享同一规范树与版本号。

## 日志与可复核结果

每次测试运行写到 `logs/runs/<TEST_RUN_TAG>/`：

- `pytest-events.jsonl`：每个用例的 nodeid、版本、成败、耗时；
- `engine-events.jsonl`：引擎各阶段（parse/validate/budget/normalize/execute/failed）
  判定依据（深度、子句数、预算上限、版本号、命中文档数、耗时），
  异常以 `status:"failed"` 落盘并继续抛出，绝不伪装成功；
- `truth-cases.jsonl`：240 个随机表达式及三套真值判定。

仓库内保留了一次完整正式运行：

- `logs/final-pytest-output.txt`：`102 passed` 完整输出；
- `logs/runs/final-20260928/`：该次运行全部 JSONL；
- `logs/curl/service-session.txt`：真实 HTTP 正常/异常调用记录；
- `logs/curl/example-client-output.txt`：示例客户端输出。

服务运行时自身的诊断写在配置的 `logs/searchdsl.jsonl`（默认）。

## 错误语义速查

| category | HTTP | 典型输入 |
|---|---|---|
| `LEXER_ERROR` | 400 | `"oops`（未闭合引号）、`a\`（悬空转义）、`""`（空短语） |
| `PARSE_ERROR` | 400 | `a AND OR b`（pos 7）、`(a OR b`（pos 1）、`a : b`（pos 3） |
| `FIELD_UNKNOWN` | 422 | `foo:bar` |
| `FIELD_TYPE` | 422 | `year:abc`、`year:"2021"` |
| `BUDGET_EXCEEDED` | 413 | 树深 > 8、子句 > 64、短语 > 16 词、词长 > 128 |
