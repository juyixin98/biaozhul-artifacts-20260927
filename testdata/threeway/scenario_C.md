# 场景 C — 未提交 vs 显式删除（手工推导）

| # | 管理者 | config | 结果 |
|---|---|---|---|
| 1 | sre | `{replicas:4, image:"w9"}` | 两字段都归 sre |
| 2 | sre | `{replicas:4}` | image **缺席** → 回退自己字段，image 删除（审计 delete/retract） |
| 3 | net | `{image:"w10"}` | 无主 image 归 net；replicas 仍 sre |
| 4 | sre | `{replicas:4}` | image 缺席但属 **net** → 原样保留，无冲突、无删除 |
| 5 | sre | `{replicas:4, image:null}` | **显式 null** 删除他人字段 → 冲突 explicit_delete_field_owned_by_other |
| 6 | sre | 同上 + force | 强制删除成功，image 易主后移除 |
| 7 | sre | `{replicas:null}` | 显式删除**自己**字段 → 干净删除，无需 force |

关键区别：

- 第 4 步（缺席他人字段）：保留，静默。
- 第 5 步（显式 null 他人字段）：冲突。
- 第 2 步（缺席自己字段）：回退删除。
- 第 7 步（显式 null 自己字段）：显式删除，审计原因与第 2 步不同
 （`explicit` vs `retract`）。
