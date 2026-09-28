# 测试执行记录

- 日期：2026-09-28
- 环境：Go 1.23.4 linux/amd64；SQLite 经 `modernc.org/sqlite v1.34.5`（纯 Go，无 CGO）
- 命令：`go test -count=1 ./...` 与 `go test -race -count=1 ./...`

## 最终结果

全部通过（`-race` 同样通过）：

```
ok  fieldapply/internal/coord     0.111s
ok  fieldapply/internal/httpapi   0.117s
ok  fieldapply/internal/merge     0.309s   # 含 400×12=4800 次随机差分应用
ok  fieldapply/internal/store     0.121s   # 内存 + SQLite 双适配器契约测试
```

24 个顶层测试（另有表驱动/子测试若干）：

| 包 | 测试 | 断言要点 |
| --- | --- | --- |
| merge | TestTwoManagersDisjointFields | 两管理者改不同字段；无关字段不丢失；份额正确 |
| merge | TestConflictReportsOwnersAndForce | 冲突路径/原管理者/current/applied；force 接管后原持有者再写也冲突 |
| merge | TestExplicitDeleteVsUncommitted | 省略自有字段=删除；未提交字段保留；changes.removed 具体路径 |
| merge | TestEqualValueSharesOwnership | 同值共享不冲突；单方释放不删除共有字段 |
| merge | TestNestedKeyedListMerge | 嵌套 keyed 列表逐字段合并、删除元素、叶子级冲突、force 仅影响目标叶子 |
| merge | TestSetAndAtomicLists | set 按成员合并；atomic 整组冲突 |
| merge | TestAtomicListConflictAndForce | 原子数组整体冲突且失败不改值；force 不波及其它字段 |
| merge | TestForceTakeoverScopedToLeaf | force 严格限定叶子 |
| merge | TestStructuralCollision | 对象↔数组冲突逐叶子报告，force 清理陈旧所有权 |
| merge | TestExplicitNullSemantics | null 按放弃意见裁剪；非拥有者的 null 不能删他人字段 |
| merge | TestInvalidInputCategory / TestStaleRevision | invalid_input 与 state_conflict 可区分；revision_stale |
| merge | **TestDifferentialVsReference** | 与**独立参考实现**（扁平叶子表+集合运算，不共享生产代码）比对 4800 次随机应用的活值/所有权/冲突 |
| store | TestStoreConformanceMemory / TestStoreConformanceSQLite | 同一契约跑两种适配器：创建、重复冲突、not_found、乐观版本、原子提交、历史、每管理者 last-applied |
| coord | TestCoordinatorEndToEnd | 真实 store 上多管理者全流程；journal 含 run id、conflict 详情、删除审计 |
| coord | TestQueueFullIsResourceExhausted | 队列饱和返回 resource_exhausted/queue_full |
| coord | TestCorruptStateIsComputationFailure | 损坏活值归 computation_failure，不 panic |
| coord | TestApplyValidation | 空 manager / 非对象配置在入队前拒绝 |
| httpapi | TestHealthAndCreate | 201 创建 |
| httpapi | TestHTTPApplyConflictForceAndHistory | 409 错误信封、force、删除、历史 |
| httpapi | TestHTTPStaleRevision | 409 revision_stale 且不写入 |
| httpapi | TestHTTPErrorCategories | 400/404/409 分类映射 |
| httpapi | TestHTTPQueueFull503 | 饱和时 503 |

另做了真实二进制冒烟（SQLite + HTTP，`examples/smoke.sh`），并人工核对了 `/owners` 与 `/history` 的具体内容以及 journal 中 `conflict` 记录的路径、owners、current/applied。

## 开发过程中出现并修复的失败（保留记录）

差分测试不是摆设——它在开发过程中真实抓到了生产代码与参考实现的多处偏差，均经分析后定位并修复：

1. **revision 哨兵错误**：合并引擎最初以 `BaseRev == -1` 表示"不检查"，零值 0 被误判为过期 → 统一为 `<=0 不检查`，协调器/HTTP 侧同步。
2. **keyed 列表的键字段漏记账**：参考实现最初没把 `name` 当作受所有权约束的叶子，差分首跑即暴露 `.spec.containers[name="web"].name` 差异 → 明确"键字段本身也是叶子"。
3. **参考实现非事务性**：参考实现遇到冲突后仍部分落盘，生产实现是全有或全无 → 参考改为两阶段（先汇总冲突，干净才提交）。
4. **force 对同值共享的处理**：参考最初在 force 时无条件剥离他人份额；正确语义是 force 只在"有差异的冲突叶子"上生效，同值只增加共享 → 两边统一。
5. **类型化 nil 切片残留为 `null`**：空 set/keyed 集合返回 `[]any(nil)` 装进非 nil 接口，活值里出现不应有的 `null` 叶 → 空集合统一返回无类型 nil，根空对象 `{}` 不产生叶子。
6. **纯所有权共享误记值变更**：以同值共享叶子最初会产生空的 changed 审计差异 → 引入独立的 `ownersMoved` 标记：revision 仍推进，但 changes 只反映值差异。
7. **HTTP schema 反序列化**：API 以字符串 `"set"/"keyed"` 声明列表种类，而内部是枚举整数 → 为 `ListKind` 增加 MarshalJSON/UnmarshalJSON（兼容字符串与数字）。
8. **SQLite 适配器 not_found 缺口**：对不存在资源的 `AppliedOf`/`History` 最初返回"空结果"而非 not_found，与内存适配器不一致 → 契约测试抓出并统一为先查 resources。

## 未执行 / 未覆盖项（如实说明）

- 未做跨进程/多实例的分布式锁与并发压测；当前串行粒度为单实例内的每资源队列，SQLite 写连接数固定为 1。
- 未做鉴权/多租户测试（`manager` 为请求自报标识，符合本地合成环境定位）。
- 未在非 amd64 平台验证（`modernc.org/sqlite` 为纯 Go，理论上跨平台，但未实际执行）。
- 历史保留策略（裁剪/归档）未实现，接口仅有读取上限。
