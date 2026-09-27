//! deletable-cuckoo：支持凭证删除的 Cuckoo 近似成员过滤器。
//!
//! 分层组织（每层只依赖其下层）：
//! - [`config`]：配置模型、文件加载与环境变量覆盖。
//! - [`hashing`]：固定、确定性的哈希内核（指纹 + 两个候选桶），与语言无关。
//! - [`filter`]：Cuckoo 桶表、有上限的迁移、失败回滚。
//! - [`token`]：删除凭证（capability token）签发与校验。
//! - [`ledger`]：每键插入计数与每令牌已花费记录。
//! - [`persistence`]：单一原子快照文件的编码/解码与崩溃安全写入。
//! - [`service`]：将以上层组合成可执行状态机（插入/查询/删除/统计）。
//! - [`api`]：Axum HTTP 适配层。
//! - [`telemetry`]：版本化、可关联运行身份的日志。

pub mod api;
pub mod config;
pub mod credentials;
pub mod filter;
pub mod hashing;
pub mod ledger;
pub mod persistence;
pub mod service;
pub mod telemetry;
pub mod token;

/// 语义版本（与 Cargo.toml 保持一致；发布时需同步）。
pub const VERSION: &str = "1.0.0";

/// 快照二进制格式版本。格式演进时必须提升。
pub const SNAPSHOT_FORMAT_VERSION: u32 = 1;

/// 删除凭证令牌版本。
pub const TOKEN_VERSION: u8 = 1;

/// 哈希内核版本：指纹/索引计算口径变化时必须提升。
pub const KERNEL_VERSION: u32 = 1;
