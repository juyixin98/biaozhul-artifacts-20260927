# SQLGuard — 受限 SQL 模板与参数绑定审查后端

SQLGuard 在**不执行待审 SQL** 的前提下，对参数化 SQL 模板做结构化审查：

- 用**手写词法分析器 + 递归下降解析器**区分字符串字面量、注释、标识符与参数占位符
  （不是对整条 SQL 做正则匹配）；
- **值参数不能出现在表名/排序字段等标识符位置**，动态标识符必须写成
  `${slot}` 并绑定到策略（policy）中声明的白名单；
- 仅针对本地**只读、不可变**的合成 SQLite 夹具读取目录（schema），绝不在任何
  连接上 `execute()` 用户 SQL；
- 每条审查都给出**结论（accept / reject / unanalyzable）、失败类别码、安全依据、
  无法分析的范围、请求标识和脱敏后的关键状态**；
- 审计记录经 Fernet 对称加密落盘，请求 id 只以 HMAC 索引形式存在。

## 目录结构

```
sqlguard/            生产代码，按真实职责分模块
  lexer.py           词法分析：字符串/引号转义/注释/四类占位符/${}槽位
  ast_nodes.py       AST 定义
  parser.py          递归下降 + Pratt 解析；不支持的语法显式报错
  policy.py          白名单策略加载（表/列/槽位角色/允许值）
  kernel.py          安全内核：规则校验、绑定类型检查、标识符槽位角色判定
  isolation.py       只读 immutable 连接 + schema 快照（状态隔离）
  crypto.py          Fernet 密钥派生与 HMAC 索引
  audit_store.py     加密审计存储（独立于夹具库）
  redaction.py       值脱敏（类型/长度/指纹）与请求 id
  config.py          环境变量配置
  service.py         装配层
  app.py             FastAPI 审计接口
  models.py          接口模型
configs/policy.json  白名单策略（独立于代码）
fixtures/shop.db     本地合成只读夹具（脚本可重建）
golden/cases.json    手工误报/漏报黄金集（独立于被测实现编写）
tests/               单元 + 集成测试（142 个）
scripts/             建库 / 测试 / 启动脚本
docs/                安全依据、API、无法分析范围说明
```

## 快速开始（首次使用者）

需要 Python 3.10+（在 3.12 上验证）。

```bash
# 1) 创建虚拟环境并安装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 构建本地合成夹具（只读模式，fixtures/shop.db）
.venv/bin/python scripts/build_fixture.py

# 3) 运行全部测试
.venv/bin/python -m pytest

# 4) 启动服务（或 bash scripts/run.sh）
.venv/bin/uvicorn sqlguard.app:app --host 127.0.0.1 --port 8080
```

一键脚本：`bash scripts/test.sh`（建库 + 测试）、`bash scripts/run.sh`（启动）。

## 使用示例

健康检查：

```bash
$ curl -s http://127.0.0.1:8080/health
{"status":"ok","ready":true,"policy_id":"shop-readonly-v1",
 "schema_digest":"9ad7dbc3c666f50a","tables":["audit_events","orders","products","users"]}
```

安全模板（动态排序字段走 `${slot}` 白名单）：

```bash
$ curl -s -X POST http://127.0.0.1:8080/api/v1/audit/reviews \
  -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT id FROM orders ORDER BY ${sort_col} ${dir_kw}",
       "identifiers":{"sort_col":"created_at","dir_kw":"DESC"}}'
{"verdict":"accept", ...}
```

值参数被放到表名位置（典型注入面）——拒绝并给出类别：

```bash
$ curl -s -X POST http://127.0.0.1:8080/api/v1/audit/reviews \
  -H 'Content-Type: application/json' \
  -d '{"sql":"SELECT id FROM ?","parameters":["orders"]}'
{"verdict":"reject",
 "findings":[{"code":"VALUE_USED_AS_IDENTIFIER", ...}], ...}
```

槽位绑定了带分号的多 token 载荷——拒绝：

```bash
curl -s -X POST .../api/v1/audit/reviews -d '{
  "sql":"SELECT id FROM ${tbl}",
  "identifiers":{"tbl":"orders; DROP TABLE users"}}'
# -> verdict=reject, code=IDENTIFIER_NOT_ALLOWED
```

## 占位符与槽位约定

| 写法 | 含义 | 绑定方式 |
|---|---|---|
| `?` | 位置值参数（按源序 1、2、3…） | `parameters: [...]` |
| `$1` | 编号值参数 | `parameters: {"1": ...}` |
| `:name` / `@name` | 命名值参数 | `parameters: {"name": ...}` |
| `${name}` | **标识符槽位**（表名/列名/关键字） | `identifiers: {"name": "..."}` |

标识符槽位的绑定必须能被词法分析成**单个、不带引号的标识符/关键字**；
空白、引号、多 token 一律拒绝。是否允许以及允许取哪些值，由
`configs/policy.json` 中该槽位的 `roles` 与 `allowed` 决定。

## 结论类别

- `accept`：在声明的策略与夹具目录下可证明满足全部规则（允许的语句、表白名单、
  列白名单、槽位白名单、绑定类型）。
- `reject`：命中明确安全规则，例如 `VALUE_USED_AS_IDENTIFIER`、
  `IDENTIFIER_NOT_ALLOWED`、`PARAMETER_TYPE_INVALID`、`TABLE_NOT_WHITELISTED`。
- `unanalyzable`：词法/语法错误或超出可建模子集（堆叠语句、子查询、CTE、
  UNION、DDL 等），**拒绝放行**，并在 `findings`/`diagnostics` 中给出精确原因
  与偏移量。

完整类别码与安全依据见 [docs/SECURITY.md](docs/SECURITY.md)；接口字段见
[docs/API.md](docs/API.md)；已知分析边界（含误报/漏报讨论）见
[docs/LIMITATIONS.md](docs/LIMITATIONS.md)。

## 测试结论（本机实际运行）

```
142 passed, 1 warning in 0.70s
```

黄金集 `golden/cases.json` 由审查者手工编写（不是由被测核心生成），包含
注入真阳性、安全真阴性、防误报、绑定校验与已知边界共 40+ 条；测试逐条断言
**具体 verdict 与失败类别码**，而不是只断言“接口能调用”。
