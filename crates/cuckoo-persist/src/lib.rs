//! # cuckoo-persist
//!
//! 持久化适配层：
//! - [`codec`]：磁盘二进制格式（magic / 版本 / 参数 / 槽位 / XXH64 校验和）；
//! - [`store`]：临时文件 + fsync + rename 的原子快照存储；
//! - [`service`]：把过滤器状态（含「凭证 -> 槽位」归属表）整体落到 [`FileStore`]，
//!   并在写盘失败时回滚内存状态。

pub mod codec;
pub mod service;
pub mod store;

pub use codec::{CodecError, Snapshot, FORMAT_VERSION, HEADER_LEN, MAGIC};
pub use service::{PersistError, PersistResult, ServiceState};
pub use store::{FileStore, StoreError, StoreResult};
