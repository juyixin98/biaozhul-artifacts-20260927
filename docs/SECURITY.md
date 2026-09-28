# 安全依据（Security Basis）

本文说明每条判定背后的规则、为什么这样设计，以及审查器“看到了什么”。
类别码是接口契约的一部分，测试直接断言它们。

## 1. 解析，而非正则猜测

词法分析器（`sqlguard/lexer.py`）是逐字符状态机，而不是对整条查询套用一组
全局正则。这决定了几件关键的事：

- `'... :x ? $1 ${t} ...'` 中的占位符是**字符串内容**，不是绑定参数；
- `-- ...` 与 `/* ... */` 中的占位符是**注释内容**；
- SQL 标准的双写单引号 `'O''Brien'` 是转义而不是字符串结束；
- 未闭合的字符串/块注释、未闭合的 `${...}` 是**硬性词法错误**
  （`LEX_ERROR`），而不是被静默接受。

这些“惰性出现（inert occurrences）”仍会记录在响应的 `inert_occurrences`
里，作为“看见了但按数据处理”的证据，而不是悄悄忽略。

解析器（`sqlguard/parser.py`）在词法 token 流上做递归下降 + Pratt 表达式
解析，产出 AST。语义校验只在 AST 上进行。

## 2. 值不能成为标识符（核心规则）

SQL 注入的核心模式之一，是把本应是**数据**的绑定值放到需要**标识符**的位置
（表名、列名、排序字段）。参数化只对“值位置”安全。因此：

- 表名位置出现 `?`/`:x`/`$1`/`@x` → `VALUE_USED_AS_IDENTIFIER`（reject）。
- `ORDER BY ?`（即便绑定的是整数列序号）→ `VALUE_USED_AS_IDENTIFIER`。
  策略要求动态排序键必须显式走 `${slot}`。
- 动态标识符只能写成 `${name}`，且：
  1. `${name}` 必须在策略中声明，否则 `IDENTIFIER_SLOT_NOT_DECLARED`；
  2. 请求必须提供同名绑定，否则 `IDENTIFIER_SLOT_UNBOUND`；
  3. 实际出现位置必须与声明角色（`table` / `column` / `keyword`）一致，
     否则 `SLOT_ROLE_MISMATCH`；
  4. 绑定字符串必须词法成**单个、无引号**的标识符/关键字，否则
     `IDENTIFIER_NOT_ALLOWED`（`orders; DROP TABLE x`、`"orders"`、
     ` orders ` 都被拒绝）；
  5. 规范化后的标识符必须落在该槽位在该作用域的 `allowed` 白名单内
     （列槽位按查询中实际 FROM 的表取每表白名单），否则
     `IDENTIFIER_NOT_ALLOWED`。

## 3. 目录与语句白名单

- 语句类型必须在 `allowed_statements` 中，否则 `STATEMENT_NOT_ALLOWED`；
  解析器不支持的语句（DDL、ATTACH、PRAGMA、WITH 等）以
  `UNSUPPORTED_SYNTAX` 进入 `unanalyzable`，即“无法证明安全 → 不放行”。
- 表必须同时存在于策略白名单与夹具 schema 快照中：
  `TABLE_NOT_WHITELISTED`。
- 每张表可声明允许的操作（如 `audit_events` 不允许 delete）：
  `TABLE_OP_NOT_ALLOWED`。
- 列必须属于该表：`COLUMN_NOT_WHITELISTED`；无法在任何 FROM 表上解析的
  裸列：`COLUMN_UNRESOLVED`；多表连接下的歧义裸列：
  `AMBIGUOUS_IDENTIFIER`（必须加表限定）。

## 4. 绑定类型检查

- 值位置：标量（string/int/float/bool/null）。缺失 `PARAMETER_UNBOUND`；
  数组传到标量位置 `PARAMETER_TYPE_INVALID`。
- 数组位置（`IN (?)` 单占位、`ANY(?)`）：必须绑定数组；空数组
  `ARRAY_EMPTY`（空 IN 列表本身是非法 SQL，且策略默认禁止）；超过
  `max_array_length` 为 `ARRAY_TOO_LONG`；元素非标量为
  `ARRAY_ELEMENT_INVALID`。
- `LIMIT :n` / `OFFSET :n`：必须是非负整数（布尔值拒绝），否则
  `LIMIT_VALUE_INVALID`。
- 多余绑定：`BINDING_UNUSED`（warning，不单独导致拒绝，但会记录）。

## 5. 不执行未知 SQL + 状态隔离

- 审查路径从不把用户 SQL 传给 `execute()`；它只被词法/语法/AST 处理。
- 唯一的数据库访问是读取夹具目录：`file:...?mode=ro&immutable=1`，
  并在连接上安装 authorizer，只允许 SELECT / PRAGMA / READ。
- 内核依据的是一次性的内存 **schema 快照**，响应 `basis.schema_digest`
  报告本次推理所用目录的摘要，保证可复现。
- 审计写在**另一个**数据库（`runtime/audit.db`），与只读夹具物理分离。

## 6. 审计、加密与脱敏

- 每条审查生成/透传 `request_id`（可由 `X-Request-Id` 指定），日志、
  响应、审计行都带它。
- 审计记录体使用 cryptography 的 **Fernet**（AES-128-CBC + HMAC）加密；
  请求 id 不入库，只存 HKDF 派生 HMAC 密钥算出的索引令牌。
- 绑定值进入响应/日志/密文前先脱敏（`redaction.py`）：只保留类型、长度、
  SHA-256 截断指纹；名称暗示敏感（`password`/`token`/`api_key`/`secret`
  等）时连短预览也不保留。相同值指纹相同，可关联但不可还原。

## 7. 无法分析范围（失败即关闭）

以下情况返回 `unanalyzable` 且不放行，而不是“猜一个 accept”：

| 类别码 | 触发 |
|---|---|
| `LEX_ERROR` | 未闭合字符串/注释/槽位、非法字符、`$0` |
| `PARSE_ERROR` | 语法不完整、token 序列不符合文法 |
| `UNSUPPORTED_SYNTAX` | 堆叠语句、子查询、CTE、UNION、DDL 等子集外结构 |
| `SCHEMA_UNAVAILABLE` | 夹具缺失或损坏 |
| `AMBIGUOUS_IDENTIFIER` | 列在多表下无法唯一定位 |
| `INTERNAL_ERROR` | 未预期内部错误（500，响应同样脱敏） |

更多边界与已知局限见 `LIMITATIONS.md`。
