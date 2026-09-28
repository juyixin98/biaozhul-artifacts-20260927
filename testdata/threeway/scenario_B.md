# 场景 B — 嵌套列表：map / set / atomic（手工推导）

Schema（`widget`）：

| 路径 | 语义 | 键 |
|---|---|---|
| ingresses | map（按键合并） | name |
| tags | set（标量集合） | — |
| servers | atomic（整表原子） | — |

所有权粒度：map → 元素内每个叶子；set → 每个标量值；atomic → 整条路径。

## 1. net 建立
`ingresses[edge-1]={host:a, port:80, tls:true}`，`tags=[canary]`，`servers=[{z1,10}]`
所有者：edge-1.host/port/tls=net，tags[^"canary"]=net，servers=net。

## 2. web 增加 edge-2 + beta
map 新元素按声明顺序追加；set 新值 beta 与 canary 并列；servers 未提及且非 web 所有 → 不动。

## 3. web 非强制改 edge-1.host（同时照报自己的 edge-2、beta）
冲突路径精确到嵌套叶子 `ingresses[name="edge-1"].host`，owners=[net]。整次拒绝。

## 4. web force 改 edge-1.host
只有该叶子易主 web；edge-1.port/tls 仍 net，edge-2 仍 web，tags/servers 不动。
审计：该路径 `takeover` from=a.example to=a-hijack.example。

## 5. net 全量声明，tags:[]（省略 canary）
canary 属于 net → 被回退；beta 属于 web → **保留**（省略他人值不是删除）。
edge-1.host 现已属 web → 保留 a-hijack.example；port/tls 仍 net 保留。

## 6. net 再报 canary、省略 beta
canary 重新归属 net；beta 虽被省略但属于 web → 保留、不报错。
最终 tags=[canary, beta]。

## 7. web 非整体替换 servers
servers 整条属 net、内容不同 → 冲突 `servers`/atomic_value_mismatch，owners=[net]。
（web 同时照报 edge-1.host、edge-2、beta，保证只触发 servers 一条冲突。）

## 8. web force 替换 servers
servers 易主 web；其余全部不变。set 输出顺序：声明值(beta)在前、保留的他人值
(canary)在后。
