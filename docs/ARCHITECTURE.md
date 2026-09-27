# 架构与持久化恢复

## 组件关系

```
            HTTP (Axum, src/api.rs)
  request-id 中间件 / 严格 JSON 归类 / i128 查询参数
                      │
                      ▼
            Registry (src/service.rs)
        RwLock<Vec<Arc<Version>>, FsStore>
          │                         │
   读者克隆 Arc<Version>     写者：校验→构造快照
   后立即释放锁                       → fsync 提交 → 压入向量
          │                         │
          ▼                         ▼
   Version (src/version.rs)  FsStore (src/store/mod.rs)
   ├─ CoordTable             ├─ events.jsonl (append, checksummed)
   ├─ DenseGrid (i64)        └─ CURRENT (rename + dir fsync)
   └─ Fenwick2D (i128)
```

## 读路径

`Registry::query` 在读锁内按版本号克隆 `Arc<Version>`（版本号越界→
`unknown_version`，库为空→`not_initialized`），随即释放锁。矩形和在不可变
快照上**无锁**计算，因此重建表不会影响旧版本读者。

## 写路径（顺序是正确性关键）

1. 取写锁；
2. 基于 head 完整校验并构造**新网格**（任何失败在此返回，状态零改动）；
3. `FsStore::commit`：规范序列化 → 计算 FNV-1a-64 → 追加一行 → flush →
   `fsync(events.jsonl)` → 写 `CURRENT.tmp` → fsync → rename 为
   `CURRENT` → fsync 目录；
4. 构造不可变 `Version`（含新 Fenwick）并压入版本向量（单次发布）；
5. 释放写锁。

读者不可能观察到“已构造但未提交”或“已提交但未发布”的版本：
- 快照只在步骤 4 对读者可见；
- 步骤 3 成功（含 fsync）后才执行步骤 4。若进程在 3 与 4 之间崩溃，重启
  重放会依据日志重建该版本，对外历史与崩溃前本应发布的一致。

## 磁盘格式

`events.jsonl`，每行一个 JSON 对象（BTreeMap 规范键序）：

```json
{"event":{"type":"batch","updates":[{"delta":3,"x":-5,"y":-3}]},"hash":"55d76094792cc577","seq":2,"version":2}
```

- `type ∈ registered | batch | rebuilt`；
- `seq` 为行号（从 1 起），`version == seq`，稠密无空洞；
- `hash = fnv1a64( 不含 hash 字段的规范 JSON )`，16 位小写十六进制；
- `CURRENT`：单个十进制版本号 + 换行。

## 启动重放

1. 逐行读取：空行/非法 JSON/缺 hash/校验和不符 → `persistence_error`；
2. `seq` 必须等于行号，`version` 必须等于 `seq`；
3. 首事件必须是 `registered`，且只能出现一次；后续为 `batch/rebuilt`；
4. 重放时用与在线写路径相同的校验函数折叠；重建确定性地从旧网格携值；
5. 交叉校验 `CURRENT == 日志尾版本`；缺失/超前/非数字均拒绝启动；
6. 打开追加句柄，下一行 seq = 行数 + 1，版本链跨重启连续（测试
   `appended_after_reopen_continues_version_chain`，verify.sh 重启后发
   v4 批次）。

## 并发

- 单进程内多线程：`std::sync::RwLock`。写串行化（保证日志 seq 与版本号
  一致），读临界区极短。查询计算在锁外，吞吐不受 Fenwick 构建影响。
- 单数据目录只允许一个服务进程写；多进程共享同一目录**不在**设计范围
  （无文件锁；见 NOT-RUN-CHECKS）。

## 内存与复杂度

设压缩后维度 `nx × ny`：

- 每版本内存：网格 `nx·ny·8` 字节 + Fenwick `(nx+1)(ny+1)·16` 字节；
  历史版本全保留，空间随版本数线性增长（本系统为离线、可完全重建历史的
  教学/验证后端）。
- 注册/重建：`O(nx·ny·log nx·log ny)` 构索引；
- 批次：校验 `O(k log n)`，构索引与注册同阶（k = 批大小）；
- 查询：`O(log nx · log ny)`，与点数无关。
