# searchdsl 文本规范 v0.1

搜索 DSL：支持字段限定、括号、AND/OR/NOT、隐式连接、短语与反斜杠转义，
输出规范查询树（canonical query tree）。

## 1. 词法

| token | 说明 |
|---|---|
| `AND` `OR` `NOT` | **大写**关键字运算符；小写 `and`/`or`/`not` 是普通词 |
| `(` `)` | 分组括号 |
| `:` | 字段限定符，`field:value` 形式，**冒号两侧不允许空白** |
| `"..."` | 短语；内容按 `[a-z0-9]+` 切词并小写化（与索引词元同口径），要求位置连续匹配 |
| 裸词 | 非空白、非 `( ) : "` 的字符序列 |

- **位置**：所有错误报告 1 起始列号（UTF-8 字符列）。
- **转义**：`\x` 一律表示字面量 `x`，因此 `\:` `\"` `\(` `\\` 可在裸词/短语中出现而不具特殊含义；结尾悬空的 `\` 是词法错误。
- 空短语 `""` 是词法错误（短语至少一个词）。
- 未闭合引号是词法错误，位置指向开引号。

## 2. 文法与优先级

```
or_expr  := and_expr (OR and_expr)*
and_expr := unary ((AND)? unary)*   # 相邻操作数之间默认隐式 AND
unary    := NOT unary | primary
primary  := '(' or_expr ')' | (TERM ':')? (TERM | PHRASE)
```

优先级：**NOT > AND（含隐式 AND）> OR**，同级左结合。

歧义裁决示例（完整 AST 见 `docs/ambiguity.md`）：

- `a OR b c`        = `a OR (b AND c)`（隐式 AND 强于 OR）
- `NOT a OR b`      = `(NOT a) OR b`（NOT 强于 OR）
- `a AND NOT b c`   = `a AND (NOT b) AND c`
- `title:"a OR b"`  中 `OR` 在引号内，是短语词，不是运算符
- `a : b` 冒号两侧有空白 → 语法错误（指向冒号列）

## 3. 字段白名单与类型（执行前校验）

字段在 `config/searchdsl.yaml` 的白名单中声明类型，解析完成后、执行前整树校验：

- 未知字段 → `FIELD_UNKNOWN`（422），位置指向字段名；
- `int` 字段的值必须匹配 `[+-]?\d+`，否则 `FIELD_TYPE`（422）；
- `int` 字段不允许短语 → `FIELD_TYPE`；
- 不带 `field:` 的词/短语在 `default_fields`（均为 text）上做 OR 匹配。

当前白名单：`title:text, body:text, author:text, tags:text, year:int`。

## 4. 规范化（逻辑化简）

规范化在预算检查之后执行，规则均保持语义且结果**幂等**：

1. 同名布尔节点拍平：`And(And(a,b),c)` → `And(a,b,c)`；
2. 重复子句删除：`a AND a` → `a`，`a OR a` → `a`；
3. 子节点按确定性键（字典序序列化）排序；
4. `NOT(NOT(x))` → `x`；
5. 单子句布尔节点脱壳：`And(x)` → `x`。

语义保留红线：

- **空查询**（空字符串）解析为 `Empty`，规范化后仍是 `Empty`，语义为匹配全部文档；
- **未知字段**在规范化*之前*被校验拦截，化简不会把未知字段静默丢弃或变成全匹配；
- 不做德摩根/分配律改写（那会改变树深和子句数，破坏预算口径）。

## 5. 复杂度预算（执行前检查）

| 预算 | 默认 | 超限时类别 |
|---|---|---|
| `max_depth` 树深（叶子=1） | 8 | `BUDGET_EXCEEDED` (413) |
| `max_clauses` 叶子子句数 | 64 | `BUDGET_EXCEEDED` |
| `max_phrase_terms` 短语词数 | 16 | `BUDGET_EXCEEDED` |
| `max_term_length` 裸词长度 | 128 | `BUDGET_EXCEEDED` |

预算在**规范化之前**对原始树检查：括号深度与未化简的嵌套都会计入，无法靠化简绕过。
空查询（深度 0、子句 0）始终通过。

## 6. 匹配语义

- 词元：`[a-z0-9]+`，索引与查询都小写化，大小写不敏感；
- int 字段按整数字面量等值匹配；
- 短语要求词序列在同一字段内位置连续；
- `AND` 取交集、`OR` 取并集、`NOT` 对**当前文档全集**取补集；
- `Empty` 匹配全部文档。

## 7. 查询树版本

规范树以紧凑 JSON（键排序）取 `sha256[:16]` 作为内容寻址版本号：
同一棵规范树（包括等价书写 `a AND b` / `b AND a`）版本号相同，
存入 SQLite `query_versions` 表，记录首次出现时间与累计运行次数。

## 8. 错误类别

| category | HTTP | 含义 |
|---|---|---|
| `LEXER_ERROR` | 400 | 未闭合引号、悬空转义、空短语 |
| `PARSE_ERROR` | 400 | 括号不平衡、运算符位置非法、字段冒号空白、查询残缺 |
| `FIELD_UNKNOWN` | 422 | 字段不在白名单 |
| `FIELD_TYPE` | 422 | int 字段非整数值 / 非文本字段上的短语 |
| `BUDGET_EXCEEDED` | 413 | 深度/子句/短语/词长超限 |

错误响应形如 `{"error": {"category", "message", "position"}}`，并带 `run_id`
（在响应头或服务日志中），可与 `logs/searchdsl.jsonl` 的诊断事件逐阶段关联。
任何异常都不会被统一成成功响应。
