# 设计说明（简化模型，非完整 Iceberg）

## 1. 快照与清单的物化

- `tables`：表名、分区列、当前 HEAD 快照、表 schema。
- `snapshots`：自增 id、父快照（线性链）、操作类型（ROOT/APPEND/OVERWRITE）、
  commit id（= request_id）。
- `manifest_rows`：**按快照物化**的 `(snapshot_id, file_relpath, row_count,
  partitions_json)`。读快照即一次主键查询，不需要沿链合并；代价是写入时
  复制未变更文件的行。本地规模下这是有意的简单化。
- `partition_changes`：`(table, partition_key, snapshot_id, op=ADD|REPLACE)`，
  冲突检测只沿 base→HEAD 的快照链查这张表。

不可变文件在旧快照中继续被引用，因此 OVERWRITE 只是让**新快照**不再包含
旧文件；旧快照仍可读。只要还有快照引用，物理文件就不是孤立文件。

## 2. 提交时间线

```
客户端                         服务
 │  POST /commits(base=N)
 │ ───────────────────────────► 1. 适配层：逐个完整读取+校验 parquet
 │                              2. 全部文件 copy 到 staging（tmp+fsync+rename）
 │                              3. 获取表级锁 + BEGIN IMMEDIATE
 │                                   - 幂等检查（request_id+载荷指纹）
 │                                   - 读 HEAD，沿链求 changed 分区
 │                                   - 相交 => REJECTED(终态) 并 409
 │                                   - 不相交 => 物化新快照行集
 │                                   - 插入快照/清单/分区变更/推进HEAD/落COMMITTED
 │                              4. 事务内把 staging 文件 os.replace 到
 │                                 warehouse/<table>/<rid>/<idx>-<name>
 │                              5. COMMIT；清理本次暂存残留（逐文件台账）
 │ ◄───────────────────────────  201 + snapshot_id, rebased, 行集统计
```

文件发布放在元数据事务**内**：发布失败则元数据回滚，已发布文件成为无引用
孤立文件并立即登记台账删除；发布成功但 COMMIT 失败在本地同一 SQLite 与
同一文件系统上不会出现“HEAD 已指向但事务回滚”的中间态（回滚后文件无引用，
由孤立扫描/对账回收）。

### 为什么物理发布可以放在事务窗口内

本地场景：SQLite 事务与 warehouse 在同一台机器、同一崩溃域。这避免了
“元数据已提交但文件缺失”的更糟状态；剩余的“文件存在但元数据回滚”是
可检测、可回收的孤立文件，有独立台账。这不是分布式两阶段提交。

## 3. 冲突规则（声明规则）

设 C = 本次提交触碰的分区集合，D = (base, HEAD] 区间内被改动分区集合。

- `C ∩ D = ∅`：接受。若 `HEAD ≠ base`，标记 `rebased=true`，新快照父节点
  直接挂 HEAD，行集 = HEAD 行集 ∪ 新文件（APPEND）或 HEAD 中目标分区被替换
  （OVERWRITE）。
- `C ∩ D ≠ ∅`：`CONFLICT_OVERLAPPING_PARTITION`，返回 base/head、
  overlapping/changed/requested 三个分区列表以便诊断。
- OVERWRITE 的每个文件必须恰好属于一个分区；一个不可变物理文件不能横跨
  “被替换”和“被保留”两个分区，否则 VALIDATION。
- 未分区表用保留键 `""` 表示，因此任意两个并发写都会冲突（安全默认）。

## 4. 幂等 / 响应丢失

载荷指纹 = sha256(表、操作、base、全部文件内容 sha256 的有序列表) 前 16 位。

- 同 request_id + 同指纹：返回存储的终态（COMMITTED→同一 snapshot_id 与
  行集统计，`replayed=true`；REJECTED/FAILED→同一错误类别）。
- 同 request_id + 不同指纹：VALIDATION（客户端把幂等键用错了）。
- 重放时仓库目标路径已存在同内容文件则消费暂存副本并视为成功；路径存在但
  内容不同返回 INDETERMINATE，绝不覆盖。

## 5. 文件完整性与清理

- 暂存：`.writing-<uuid>` 临时文件写完、flush+fsync 后 `replace` 成正式
  暂存名，并对目录 fsync；读者永远看不到半成品。
- 发布：目标路径含随机/请求隔离段且不可变；`os.replace` 原子挂载。
- 独立清理台账 `cleanup.sqlite3`：`record_id, request_id, table, kind,
  path, status(PENDING/REMOVED/FAILED), recorded_at, removed_at, error`。
  与元数据库分离——元数据故障不会销毁“哪些文件需要回收”的审计线索。
- 类型：`STAGED_TEMP_FILE`、`STALE_STAGED_FILE`、
  `PARTIAL_PUBLISH_ORPHAN`、`WAREHOUSE_ORPHAN`。

## 6. 明确不做的事（与 Iceberg 的差距）

无 manifest list/metadata.json 多文件元数据格式；无 schema evolution；
无 delete files/merge-on-read；无 REST catalog 与表属性；分区值不做
Iceberg 的 transform（identity/transform），只用规范化字面量；无远程/
对象存储与跨进程原子 rename 协调；并发串行化依赖单 SQLite 写锁与进程内
表锁，不适合多进程写入（多进程由 SQLite 写锁保证正确性，但重放合并的
吞吐不在目标内）。
