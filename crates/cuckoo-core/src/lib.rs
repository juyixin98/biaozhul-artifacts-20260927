//! # cuckoo-core
//!
//! 可删除 Cuckoo 近似成员过滤器的纯逻辑内核：
//!
//! - [`params`]：固定格式参数与校验；
//! - [`hashing`]：指纹与两个候选桶的固定计算（格式 v1 内核）；
//! - [`rng`]：确定性迁移随机源（SplitMix64，可复现）；
//! - [`filter`]：桶/槽存储、有上限的迁移、失败整体回滚、槽位删除；
//! - [`credential`]：与键绑定的删除凭证（HMAC token）。
//!
//! 本 crate 不做 I/O、不持有锁、不依赖 HTTP，便于独立审查与测试。

pub mod credential;
pub mod error;
pub mod filter;
pub mod hashing;
pub mod params;
pub mod rng;

pub use credential::{issue as issue_token, verify as verify_token, JTI_LEN};
pub use error::{CoreError, CoreResult, FailureKind};
pub use filter::{CuckooFilter, InsertTrace, Placement, Swap};
pub use hashing::{alt_index, fingerprint_and_i1, locate, xxhash64};
pub use params::FilterParams;
pub use rng::SplitMix64;

/// 存储格式版本。任何哈希/编码语义的不兼容变更都必须抬升版本。
pub const FORMAT_VERSION: u32 = 1;
/// 代码版本（独立测试日志会打印）。
pub const CORE_VERSION: &str = env!("CARGO_PKG_VERSION");
