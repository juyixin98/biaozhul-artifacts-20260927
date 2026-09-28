# 分析范围、误报与漏报说明

审查器的目标是**对可建模的子集给出可证明的结论**，对子集外的结构明确报
`unanalyzable`，而不是给出可能错误的 accept。本文列出边界，便于接入方正确使用。

## 支持的 SQL 子集

- 单条 `SELECT` / `INSERT` / `UPDATE` / `DELETE`，允许结尾一个分号；
- 比较/逻辑/算术、`IN (值列表)`、`ANY(:arr)`/`SOME(:arr)`、`BETWEEN`、
  `IS [NOT] NULL`、`LIKE/GLOB`、`CASE`、`CAST`、普通函数调用、
  `COUNT(*)`、`JOIN ... ON/USING`、`GROUP BY/HAVING`、`ORDER BY`、
  `LIMIT/OFFSET`；
- 占位符：`?`、`$1`、`:name`、`@name`；标识符槽位：`${name}`；
- 标识符：裸标识符、双引号 / 反引号 / 方括号引号标识符。

## 明确不支持（报 `UNSUPPORTED_SYNTAX` → 不放行）

- 多条堆叠语句（`a; b`）；
- 子查询（标量子查询、`IN (SELECT ...)`、`EXISTS`、FROM 子查询）；
- CTE（`WITH ...`）、集合运算（`UNION/INTERSECT/EXCEPT`）；
- 窗口函数、触发器/视图/DDL（`CREATE/DROP/ALTER/...`）、`PRAGMA`、
  `ATTACH`、事务控制语句；
- 方言专有转义（PostgreSQL E-strings、MySQL `\` 转义等）——SQLite 方言。

需要扩展时，正确做法是在 `parser.py`/`ast_nodes.py` 增加对应模型与内核规则，
而不是放宽现有检查。

## 已知的非问题（避免误报）

- **占位符出现在字符串或注释里**：词法器归类为数据/注释，记录到
  `inert_occurrences` 作为证据，不消耗绑定、不产生拒绝。黄金集 `FP-001`
  至 `FP-004` 锁定该行为。
- **绑定值 `NULL`**：是合法标量（`FP-005`）。
- **值占位符出现在比较谓词左侧**（`:min <= total_cents`）：这是合法的
  参数化谓词——值与固定列比较，不是标识符拼接（`LIMIT-001`）。
- **白名单内的引号标识符**（`SELECT "id" FROM "orders"`）仍是合法标识符
  （`FP-006`）；但**槽位绑定**不接受带引号输入，因为策略要求绑定只能是单个
  裸标识符，引号可能携带额外语义。

## 已知的分析局限（诚实声明）

1. **语义级数据流不分析**。审查器保证的是“模板结构 + 绑定类型/白名单”，
   不会判断业务上是否“应该”允许某列被查询或更新；那是策略表
   （`tables.<t>.allow`、槽位 `allowed`）的职责。
2. **表达式别名在 ORDER BY 中**按固定列解析；当前不对 `SELECT
   expr AS x ... ORDER BY x` 的输出别名做名字解析（属于可扩展项，不是安全
   缺口——未知标识符会按 `COLUMN_UNRESOLVED` 拒绝，fail-closed）。
3. **不解析 SQL 函数内部语义**（如不评估 `printf`/字符串拼接的结果）。
   标识符位置只接受静态标识符或 `${slot}`，因此拼接函数无法被用作表名/列名
   旁路；但审查器也不证明某个值级函数的业务安全性。
4. **schema 漂移**：策略与夹具目录不一致时，以夹具快照为准并可能给出
   `COLUMN_UNRESOLVED` 警告/错误。接入方应保持策略与真实只读目录同步。
5. **不执行任何 SQL**：因此无法基于真实查询计划判定行数/性能，也不会发现
   只有运行时才出现的错误（如类型亲和性导致的运行时转换）。本服务定位是
   安全审查，不是数据库测试框架。
6. **密钥管理**：演示部署的主密钥是本地随机文件（0600）。生产部署应替换为
   KMS/环境注入；`crypto.KeyMaterial` 接受任意 >=16 字节主密钥。

## 黄金集如何覆盖误报/漏报

`golden/cases.json` 由审查者手工编写，分组包括：

- `true_positive_injection`（TP）：必须拒绝/无法分析的攻击与越权；
- `true_negative_safe`（TN）：必须接受的合法参数化用法；
- `false_positive_guards`（FP）：容易被朴素正则方案误报、本系统必须接受的
  场景（引号转义、注释内占位、`NULL` 等）；
- `binding_validation`（RX）：具体绑定错误类别；
- `warnings` / `known_limitations`：警告与文档化边界。

任何规则变更导致黄金集失败时，必须先解释是“修复了真正的漏报”还是“引入了
误报”，并同步更新黄金集与本文档——不允许删除用例来让测试变绿。
