//! # rb-persist
//!
//! [`RoaringSet`] 的文件系统持久化适配层：
//!
//! - 所有集合存放在一个 **基础目录** 下，文件名为 `<安全名>.rbs`；
//! - 写入采用“临时文件 + `fsync` + 原子 rename”，崩溃不会留下半截文件；
//! - 加载时走 [`rb_format::codec`] 的严格校验，损坏文件被原样拒绝；
//! - 额外支持 JSON 夹具导入（整数数组），便于测试与外部工具产出数据。
//!
//! 名称只允许 `[A-Za-z0-9_-]`，禁止路径分隔与 `..`，防止目录逃逸。

#![forbid(unsafe_code)]

pub mod fixtures;
pub mod naming;
pub mod store;

pub use store::{Store, StoreError};
