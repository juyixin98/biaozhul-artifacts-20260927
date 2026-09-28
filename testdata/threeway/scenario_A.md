# 场景 A — 两个管理者、字段冲突与强制接管（手工三方合并推导）

本文件是 `internal/merge/golden_a_test.go` 中期望值的**独立手工推导**。
测试里的所有 `wantLive` / `wantOwners` 都来自下面这张表，而不是从被测实现
复制输出。规则只有三条：

1. 字段由**最后写入它的管理者**所有；所有权表就是三方合并里的 "base"。
2. 一次 apply 的 config 是该管理者的**全量声明**：
   - 出现且非 null → 写入；
   - 出现且为 `null` → 显式删除（与"不出现"不同）；
   - 不出现 → 只回退**自己**拥有的字段；他人字段原样保留、不报错。
3. 写入/删除命中他人字段 → 冲突（返回路径与原管理者），整次提交不落盘；
   `force=true` 才接管。

初始：无资源。

## 步骤 1 — net 建立

config: `{image:"v1", ingresses:[{name:"edge-1", host:"a.example", port:80}]}`

| 字段 | 值 | 所有者 |
|---|---|---|
| image | registry/widget:1 | net |
| ingresses[name="edge-1"].host | a.example | net |
| ingresses[name="edge-1"].port | 80 | net |

（`name` 是元素身份键，编码在路径里，本身不占所有权。）

## 步骤 2 — sre 建立不相交字段 replicas

config: `{replicas:3}`

replicas 无主 → sre 直接取得。net 的三个字段不在声明里且不属于 sre → 原样保留。
所有者：replicas=sre，image/host/port=net。

## 步骤 3 — sre 改自己的 replicas 3→5

命中自己的字段，无冲突。其余字段不动。

## 步骤 4 — sre 非强制写 net 的 image

config: `{replicas:5, image:"v2"}`

image 所有者是 net，且值不同 → 冲突：

```json
{"path":"image","owners":["net"],"reason":"atomic_value_mismatch","wanted":"set"}
```

**整次 apply 拒绝**，replicas 也不提交（事务语义），revision 不增长。

## 步骤 5 — sre 强制接管 image

同上请求加 `force:true`。image 所有者 net→sre，值 v1→v2（审计记 `takeover`，
from=v1,to=v2）。host/port 仍是 net，replicas 仍是 sre，均无变化。

## 步骤 6 — net 重报原声明

config 里 image:"v1" 与 sre 的 v2 冲突；replicas 不在 net 声明里、也不属于 net，
因此**不产生 replicas 冲突**（未提交 ≠ 删除/抢占）。只报 image 一条，owners=[sre]。
