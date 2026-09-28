# 输入语言参考（JSON）

所有程序是一个 JSON 对象：

```json
{
  "width": 8,
  "overflow": "wrap",
  "inputs": [{ "name": "x", "low": 0, "high": 255 }],
  "vars":   [{ "name": "i", "value": 0 }],
  "body": [ ...语句... ]
}
```

## 顶层字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `width` | 是 | 整数位宽：`8` / `16` / `32` / `64` |
| `overflow` | 否 | `wrap`（默认，二进制补码回绕）或 `trap`（加减乘溢出、`INT_MIN/-1`、`-INT_MIN` 为运行失败） |
| `inputs[]` | 否 | 符号输入：`name`、`low`、`high`（闭区间，无符号 w 位值，low≤high） |
| `vars[]` | 否 | 预置状态变量：`name`、`value`（初值，超位宽报错） |
| `body[]` | 是 | 语句序列；空 body 合法 |

校验规则：输入名/变量名不可重复；不可向输入赋值（赋值可隐式引入状态变量）；
表达式引用未声明名字会在解析期报错；非法位宽/运算符/溢出模式均为 400。

## 语句

```jsonc
{"stmt": "assign", "target": "y", "expr": <表达式>}

{"stmt": "if", "cond": <表达式>,
 "then": [ ... ], "else": [ ... ]}      // then/else 可省略，视为空

{"stmt": "while", "cond": <表达式>, "body": [ ... ]}

{"stmt": "assume", "cond": <表达式>}    // cond 为 0 的路径不可行
{"stmt": "assert", "cond": <表达式>}    // cond 为 0 即安全违规
```

语句在降低阶段按**先序**分配唯一 `id`（从 0 开始），失败位置/日志都用该 id。

## 表达式

每个表达式节点形如 `{"expr": <tag>, ...}`。

| tag | 字段 | 含义 |
| --- | --- | --- |
| `int` | `value`（任意整数，按 2^w 取模） | 字面量（别名 `const`） |
| `var` | `name` | 输入或变量 |
| 二元运算 | `lhs`, `rhs` | 见下表 |
| `neg` / `not` | `arg` | 补码取负 / 按位取反（别名 `-` / `~`） |
| `ite` | `cond`, `then`, `else` | cond 非零取 then，否则 else；未选中分支中的除零等副作用受条件保护 |

### 二元运算（别名也可用常见符号）

| 类别 | 运算符 |
| --- | --- |
| 算术 | `add(+)` `sub(-)` `mul(*)` `udiv(/)` `urem(%/umod)` `sdiv` `srem(smod)` |
| 位运算 | `and(&)` `or(|)` `xor(^)` `shl(<<)` `lshr(>>/ushr)` `ashr` |
| 无符号比较（结果 0/1） | `eq(==)` `ne(!=)` `ult(<)` `ule(<=)` `ugt(>) `uge(>=)` |
| 有符号比较 | `slt` `sle` `sgt` `sge` |

语义要点：

* 真值：非零即真；比较结果为 0/1。
* 移位量按位宽取模（与 SMT-LIB `bvshl/bvlshr/bvashr` 一致）。
* 除以零：求值结果取 0，同时在路径上产生 `div_by_zero` 失败守卫；
  符号分析会证明“除数≠0 不可达”时才报告违规。
* `sdiv/srem` 的 `INT_MIN / -1`：wrap 下为回绕值并置溢出标志，trap 下为
  `overflow` 失败。
* `ashr` 对有符号解释做算术右移。

## 最小示例

```json
{
  "width": 8,
  "inputs": [{ "name": "x", "low": 0, "high": 255 }],
  "body": [
    {"stmt": "assign", "target": "y",
     "expr": {"expr": "add", "lhs": {"expr": "var", "name": "x"},
              "rhs": {"expr": "int", "value": 100}}},
    {"stmt": "assert",
     "cond": {"expr": "ugt", "lhs": {"expr": "var", "name": "y"},
              "rhs": {"expr": "int", "value": 200}}}
  ]
}
```
