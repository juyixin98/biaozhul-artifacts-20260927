# `.pnet` 文本输入语言

`net_text` 字段接受一种小型声明式文本格式，与 JSON 网描述等价（初标识仍在请求体中以
库所名映射给出）。示例见 `fixtures/mutex.pnet`。

## 词法

- 大小写敏感关键字：`place` `transition` `capacity` `consumes` `produces`
- 标点：`:` `;` `,`
- 标识符：字母或下划线开头，后接字母、数字、下划线
- 整数：非负十进制（语义层再施加安全上界，拒绝 0 权弧）
- 注释：`#` 到行尾；空白与换行自由

## 语法

```
place <名称> : capacity <非负整数> ;

transition <名称>:
    [consumes <数量> <库所> (, <数量> <库所>)* ;]
    [produces <数量> <库所> (, <数量> <库所>)* ;]
```

- 两个子句顺序不限，各至多出现一次；均可为空（`consumes;` 表示无输入弧）。
- 同一变迁对同一库所的重复弧、未知库所引用、0 权弧等由统一语义校验拒绝，
  返回与 JSON 通道相同的稳定错误码；词法/语法错误带 `line:column` 位置。

## 示例

```
# 单缓冲区生产消费
place buffer : capacity 5;

transition produce:
    consumes;
    produces 2 buffer;

transition consume:
    consumes 3 buffer;
    produces;
```

对应 JSON：

```json
{
  "places": [{"name": "buffer", "capacity": 5}],
  "transitions": [
    {"name": "produce", "inputs": [], "outputs": [{"place": "buffer", "weight": 2}]},
    {"name": "consume", "inputs": [{"place": "buffer", "weight": 3}], "outputs": []}
  ]
}
```
