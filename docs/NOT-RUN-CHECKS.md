# 未执行 / 不声称通过的检查

以下检查**未**在交付环境中执行，因此不写成“已通过”。列出前提与建议的验证
方式，供有需要时补做。

| 检查 | 为什么没做 | 若要验证 |
|---|---|---|
| 断电 / `kill -9` 瞬间崩溃的耐久性（页面缓存丢失） | 容器内无法制造真实掉电；普通 `kill` 不会丢已 `fsync` 的写入 | 用 `fsync` 跟踪（e.g. `strace -e fsync,fdatasync,rename`）确认提交路径，再在故障注入环境（断电盘 / `fail_make_request`）回放 |
| 网络文件系统（NFS、SMB）上的 rename+fsync 语义 | 无挂载点；NFS 对目录 fsync/rename 的持久性依赖服务器配置 | 在目标 NFS 版本与挂载选项下做崩溃矩阵测试 |
| 多进程写同一 `data_dir` 的互斥 | 设计为单写者，未实现文件租约/锁（`flock`/`lockf`） | 若需要，先在 `FsStore::open` 加排他锁并补测试 |
| 性能基准 / 大规模数据（千万级点、长版本链） | 题目要求功能正确性与可维护性，未给性能目标；保留全历史内存随版本线性增长 | 用确定性合成基准（固定种子生成注册集/批次/查询）压测，记录 p50/p99 |
| HTTP 鉴权 / TLS / 限流 | 本地离线验证后端，默认只监听 127.0.0.1，无账号体系 | 放到反向代理后或加入 mTLS 后再补安全测试 |
| 时钟回拨对 `published_at_ns` 的影响 | 仅用于诊断；单调版本号不依赖墙钟 | 如需严格排序时间戳，改用单调时钟 + 启动时间合成 |
| `PREREG2D_LOG` 自定义过滤的运行期热加载 | 配置仅启动时读取一次 | 需要时增加 SIGHUP 重载 |
| Windows 目录 fsync | 仅在 unix 分支 fsync 目录；Windows 下 rename 原子性未在此环境验证 | 在 Windows 目标上跑 `verify.sh` 的等价流程 |
| 依赖供应链深度审计 / `cargo audit`  advisories | 交付环境未联网到 advisory 数据库（依赖经镜像拉取） | 在有网环境执行 `cargo deny check advisories` 并固定结果 |

已执行且**可复现**的检查：`./scripts/verify.sh`（fmt、clippy `-D
warnings`、16 个确定性测试、真实 HTTP 冒烟、停服重开后的持久化与旧版本
复核、重启后继续发布 v4）。运行身份形如 `verify-<UTC时间戳>-pid<PID>`，
测试日志 `target/<run>-tests.log`、服务日志 `target/<run>-server.log`。
