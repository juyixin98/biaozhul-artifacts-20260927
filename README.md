# SQLGuard — 受限 SQL 模板与参数绑定审查后端

对**参数化 SQL 模板**做静态 + 规划期审查，识别两类核心风险：

1. **值参数被当作标识符**（`?` / `:name` 出现在表名、列名、排序字段位置）；
2. **动态标识符拼接**（`{{ slot }}` 必须命中声明白名单，且关系名必须存在于只读夹具目录）。

审查过程**不执行未知 SQL**：提交的语句不会在任何数据库上运行。唯一与 SQLite
引擎的交互是在一个本地合成夹具上以**不可变只读**方式调用
`EXPLAIN QUERY PLAN`（绑定 NULL，仅让规划器做名字解析，不取数据、不改数据）。

---

## 1. 它能保证什么、不能保证什么（安全依据与分析边界）

### 审查依据（按流水线顺序）

| 阶段 | 模块 | 依据 |
|---|---|---|
| 词法 | `core/lexer.py` | 真正的**状态机分词器**，不是字符串正则。字符串字面量（`''` 转义）、双引号/反引号/方括号标识符、行/嵌套块注释、`?`/`?NNN`/`:n`/`@n`/`$n` 占位符、`{{ slot }}`、数字、blob 各自成词。字符串与注释内的 `? :name` **不会**被识别为参数。 |
| 结构 | `core/parser.py` | 递归下降解析 SELECT/INSERT/UPDATE/DELETE，恢复每个参数与槽位的**语法上下文**（`where` / `values` / `set_lhs` / `order` / `in_list 展开位` / 关系名位）。无法解析的区域显式产生 `PARSE_REGION_UNANALYZABLE`，绝不猜测放行。 |
| 策略 | `core/policy.py` | 声明白名单：允许的语句类型、可写表、每个 `{{ slot }}` 的允许标识符集合/默认值/作用域、每个值参数的允许取值集合、数组开关。白名单是唯一标识符来源，内核不"发明"标识符。 |
| 裁决 | `core/kernel.py` | 形态规则（无 WHERE 的批量 UPDATE/DELETE、不可写目标、未知表/列）、绑定规则（缺失绑定、类型、数组只能进 IN-list、空数组、取值白名单）、槽位规则（未声明/缺值/不在白名单/不在目录）、渲染后 `EXPLAIN QUERY PLAN` 结构校验。 |
| 隔离 | `state/fixture.py` | 夹具以 `file:…?mode=ro&immutable=1` 打开；额外注册 SQLite authorizer，**禁止 ATTACH/DETACH**（堵住以 `?mode=rw` 重新挂接同一文件的旁路）、PRAGMA 白名单、写动作 IGNORE（规划仍可进行，实际写入由不可变挂载拒绝）。 |
| 审计 | `state/audit.py` | 每次审查追加一条仅含**脱敏**证据的记录，HMAC-SHA256 哈希链（`chain_n = HMAC(k, chain_{n-1} ‖ canonical(记录))`），任何改行/删行/重排都会被 `verify-chain` 检出。 |

### 三类裁决

- **accept** — 通过全部规则，给出渲染后的语句（值仍为 `?`，槽位已安全引用）。
- **reject** — 命中明确拒绝规则，返回具体失败码（见下"失败码目录"）。
- **unanalyzable** — 词法/语法无法判定（如未闭合字符串）。**无法判定即不放行**，
  但与"确认恶意"区分开，便于调用方决定是修复模板还是告警。

### 明确的分析边界（`coverage.skipped` / 诊断中如实报告）

- 只审查 SQLite 兼容 DML 方言；DDL、PRAGMA、ATTACH、多语句直接拒绝。
- 派生表（子查询）内部的列不做逐列静态解析（关系检查标记 `skipped: subquery`），
  但其中的值参数仍参与绑定校验，整体仍过规划器。
- 不做表达式类型推断、窗口帧语义、触发器/视图内部权限分析。
- 规划器校验证明的是"渲染后的形态在此夹具上结构合法、名字可解析"，
  不代表该语句在任意其它 schema 上合法，也不构成对值内容的业务校验。

### 关键失败码（完整目录见 `core/models.py`）

```
VALUE_PARAM_AS_IDENTIFIER   值参数出现在表名/列名/排序字段位置
SLOT_UNDECLARED             {{ slot }} 未在策略中声明
IDENTIFIER_NOT_WHITELISTED  动态标识符不在声明白名单
MISSING_IDENTIFIER          槽位缺值
MISSING_BINDING / UNUSED_BINDING (advisory)
ARRAY_PARAM_IN_SCALAR_CONTEXT / EMPTY_EXPANSION
INVALID_PARAM_TYPE / PARAM_VALUE_NOT_ALLOWED
UNKNOWN_TABLE               静态/动态表或静态列不在夹具目录
TARGET_TABLE_NOT_WRITABLE / MISSING_WHERE
MULTIPLE_STATEMENTS / STATEMENT_TYPE_NOT_ALLOWED
RENDERED_SQL_INVALID        渲染后的参数化语句被 SQLite 规划器拒绝
LEX_ERROR / PARSE_REGION_UNANALYZABLE / EMPTY_TEMPLATE  (无法判定/拒绝)
```

---

## 2. 工程结构（模块各有真实职责，非单文件脚本）

```
sqlguard/
  core/
    lexer.py        # 词法状态机：字面量/注释/标识符/占位符/槽位
    parser.py       # 递归下降结构解析：参数与槽位的语法上下文
    models.py       # Statement/Finding/Coverage + 失败码目录
    policy.py       # 声明式白名单策略（YAML/JSON + 请求级覆盖）
    kernel.py       # 安全内核：形态/绑定/槽位/目录/规划器裁决
    redaction.py    # 脱敏：只记录类型、长度、符号、成员判定
  state/
    fixture.py      # 不可变只读 SQLite 夹具 + authorizer 隔离
    audit.py        # append-only 审计 + HMAC 哈希链 (cryptography PBKDF2)
  api/
    schemas.py      # Pydantic 请求/响应模型
    app.py          # FastAPI：/review、/audit、/audit/chain/verify、/health
  config.py         # 环境变量 + config/settings.yaml
  service.py        # 编排：policy → kernel → audit → log
  logging_setup.py  # JSON 结构化日志（带请求 id、脱敏字段）
  cli.py            # 不启服务时的命令行审查
config/
  policy.yaml       # 白名单策略（证据式配置）
  settings.yaml     # 路径/端口
samples/fixture/
  schema.sql        # 本地合成夹具 DDL（独立编写）
  seed.sql          # 合成假数据
scripts/init_fixture.py
tests/
  test_lexer.py     # 引号转义/注释占位符/嵌套注释……精确 token 断言
  test_parser.py    # 参数/槽位上下文、失败类别、位置
  test_kernel.py    # 裁决、失败码、诊断、coverage、不执行
  test_isolation.py # 只读隔离、ATTACH 旁路、审计链篡改检测
  test_redaction.py # 脱敏不泄漏
  test_policy.py    # 策略加载与请求级覆盖
  test_api.py       # HTTP 端到端集成测试
  golden/golden_cases.yaml  # 手工标注的误报/漏报黄金集（46 例）
```

黄金集的预期答案由安全需求**手工标注**于 YAML，被测内核不读取也不生成它
（有测试显式保证），因此它是独立 oracle，而不是"用被测代码给自己出卷子"。

---

## 3. 首次运行

需要 Python 3.10+（开发环境为 3.12）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 1) 生成本地合成只读夹具（从 schema.sql + seed.sql）
PYTHONPATH=. python scripts/init_fixture.py

# 2) 跑全部测试
python -m pytest -q

# 3) 命令行审查
PYTHONPATH=. python -m sqlguard.cli review \
  --template "SELECT id, name FROM users WHERE id IN (?) AND role = :role" \
  --params '{"0":[1,2,3],"role":"admin"}'

# 4) 启动 HTTP 服务
PYTHONPATH=. python -m uvicorn sqlguard.api.app:app --host 127.0.0.1 --port 8080
```

### HTTP 用法

```bash
# 接受：动态排序字段走槽位白名单，值走 ?
curl -s -X POST http://127.0.0.1:8080/api/v1/review \
  -H 'Content-Type: application/json' \
  -d '{
    "template": "SELECT id FROM users ORDER BY {{ sort_col }} {{ sort_dir }} LIMIT ?",
    "params": {"0": 20},
    "slots":  {"sort_col": "email", "sort_dir": "DESC"}
  }'

# 拒绝：? 不能当排序字段
curl -s -X POST http://127.0.0.1:8080/api/v1/review \
  -H 'Content-Type: application/json' \
  -d '{"template":"SELECT id FROM users ORDER BY ?","params":{"0":"name"}}'

# 取审计记录 / 校验哈希链
curl -s http://127.0.0.1:8080/api/v1/audit/req_xxx
curl -s http://127.0.0.1:8080/api/v1/audit/chain/verify
```

### 模板约定

- **值**一律用占位符：`?`（按出现顺序，键 `"0"`/`"1"`…）、`?NNN`、`:name`、`@name`、`$name`；
  绑定走数据库参数绑定，永不字符串拼接。
- **动态标识符**（表名/列名/排序字段）一律用 `{{ slot_name }}`，其取值必须在
  `config/policy.yaml` 对应槽位的 `allowed` 白名单内；关系名还必须在夹具目录中。
- 排序方向这类 SQL 关键字槽位用 `quote: false`，渲染为裸词（白名单仅 `ASC/DESC`，
  渲染时再次做严格词形校验），绝不加引号伪装成标识符。

### 密钥说明

审计 HMAC 密钥解析顺序：环境变量 `SQLGUARD_AUDIT_SECRET`（开发/测试便利，
固定盐 PBKDF2 派生），否则首次启动在 `data/audit.key` 生成随机密钥（0600）。
**生产环境请通过环境变量注入密钥，勿使用仓库内任何默认值。**

---

## 4. 诊断长什么样

每条 `findings` 都带失败码、**源码位置**（offset/line/col）、语法上下文与
结构化 detail；`param_diagnostics` 对每个参数说明 `accepted/rejected` 及原因。
日志与审计只含**脱敏**信息（类型、长度、符号、是否数组成员），例如值参数
`"customer-pii@example.test"` 在任何记录中只表现为
`{"type":"str","length":24}`，原始值不写库、不打日志。

`coverage` 段如实列出已检查的关系/列及 `skipped` 原因（如派生表），
这是"为什么接受 / 为什么拒绝 / 为什么无法判定"的可追溯依据。

---

## 5. 真实测试命令与结论

```bash
$ python -m pytest -q
178 passed
```

覆盖的重点攻击/误用形态：引号双写逃逸、注释内占位符、`?` 替代表名/列名/
排序字段、数组参数（IN-list 合法 / 标量位置拒绝 / 空数组拒绝）、动态排序字段、
槽位未声明/越权取值、缺绑定/多余绑定、取值白名单、多语句、DDL/PRAGMA、
未闭合字符串（无法判定）、无 WHERE 批量改写、不可写目标、未知表/列、
只读 ATTACH 旁路、审计链改行/删行/换密钥检测。
