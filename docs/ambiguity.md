# 歧义样例：明确 AST 与错误位置

位置列均为 1 起始。AST 中 `T(field,value)` 表示 Term、
`P(field,[...])` 表示 Phrase、`NOT` 表示 Not；叶子上的 `pos` 省略，详见 JSON。

## A. 优先级与隐式连接词

### A1. `a OR b c` —— 隐式 AND 强于 OR

```
OR
├── T(value=a)
└── AND
    ├── T(value=b)
    └── T(value=c)
```

规范化（排序后键序）：`OR(AND(T(b),T(c)), T(a))`。

### A2. `NOT a OR b` —— NOT 只作用于 a

```
OR
├── NOT
│   └── T(value=a)
└── T(value=b)
```

规范化：`OR(NOT(T(a)), T(b))`（`child` 键序在 `value` 前，NOT 排第一）。

### A3. `a AND NOT b c` —— NOT 仅取 b；c 是隐式 AND 的第三个子句

```
AND
├── T(value=a)
├── NOT
│   └── T(value=b)
└── T(value=c)
```

### A4. `a AND b OR c AND d` —— OR 在最外层

`OR(AND(T(a),T(b)), AND(T(c),T(d)))`

### A5. 括号改变优先级：`a AND (b OR c)`

```
AND
├── T(value=a)
└── OR
    ├── T(value=b)
    └── T(value=c)
```

## B. 引号内符号不是运算符

### B1. `title:"a OR b"`

短语查询，`OR` 是短语的第二个词，整体只有一个叶子：

```
P(field=title, terms=[a, or, b], pos=7)
```

`pos=7` 是短语（值）起始列；字段类错误会定位到字段名起始列 1。

### B2. 裸词转义：`c:\windows` 与 `"say \"hi\""`

- `c\:windows` → 单个 Term，值为字面量 `c:windows`（冒号不是字段限定符）；
- `"say \"hi\""` → 一个 Phrase，词序列 `[say, hi]`，引号内符号失去特殊含义。

### B3. 小写关键字是普通词

`salad and pie` = `AND(T(salad), T(and), T(pie))`，不是 `salad AND pie`。

## C. 错误位置

| # | 输入 | category | position | 判定依据 |
|---|---|---|---|---|
| E1 | `a AND OR b` | PARSE_ERROR | **7** | AND 之后要求操作数，位置 7 是 OR |
| E2 | `(a OR b` | PARSE_ERROR | **1** | 位置 1 的 `(` 未闭合 |
| E3 | `a : b` | PARSE_ERROR | **3** | 冒号位置 3，字段限定符两侧不允许空白 |
| E4 | `title:` | PARSE_ERROR | **7** | 冒号在位置 6，值应从位置 7 开始但查询结束 |
| E5 | `foo:bar` | FIELD_UNKNOWN | **1** | 字段名 foo 起始列，foo 不在白名单 |
| E6 | `year:abc` | FIELD_TYPE | **1** | year 为 int，abc 不是合法整数 |
| E7 | `"unterminated` | LEXER_ERROR | **1** | 开引号位置 1 未闭合 |
| E8 | `a\` | LEXER_ERROR | **2** | 悬空反斜杠在位置 2 |
| E9 | `""` | LEXER_ERROR | **1** | 空短语 |
| E10 | `(a OR b))` | PARSE_ERROR | **9** | 完整表达式后位置 9 还有多余 `)` |
| E11 | `a AND (b OR )` | PARSE_ERROR | **13** | 位置 13 是 `)`，OR 后缺少操作数 |
| E12 | 9 层嵌套 `a AND (b AND (c ...))` | BUDGET_EXCEEDED | — | 树深 9 > 8（预算在化简前计算） |
| E13 | 65 个词隐式 AND | BUDGET_EXCEEDED | — | 子句数 65 > 64 |

## D. 空查询与未知字段的化简语义

- 空字符串 → `Empty` → 规范化仍为 `{"type":"empty"}` → 执行命中全部 10 篇夹具文档；
- `foo:bar` 在校验阶段（规范化之前）即 `FIELD_UNKNOWN` 失败，
  绝不会因化简把未知字段丢掉而“成功”；
- `apple AND apple` → 规范树 `AND` 去重为单叶子 `T(apple)`，真值与化简前一致。
