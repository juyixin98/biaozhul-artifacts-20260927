//! 字节文本 FM 索引后端服务。
//!
//! 模块边界：
//! - [`alphabet`]：字节/哨兵唯一编码（数据格式契约）；
//! - [`suffix_array`] + [`bwt`] + [`rank`] + [`fm`]：索引内核（SA、BWT、Occ、后向搜索/LF/定位）；
//! - [`naive`]：独立朴素扫描参照，仅供测试与 verify，绝不参与查询路径；
//! - [`persist`]：文件系统持久化适配与损坏检测；
//! - [`service`]：索引注册表与业务编排；
//! - [`api`]：Axum HTTP 接口与统一错误契约；
//! - [`config`]：启动配置。

pub mod alphabet;
pub mod api;
pub mod bwt;
pub mod config;
pub mod error;
pub mod fm;
pub mod naive;
pub mod persist;
pub mod rank;
pub mod service;
pub mod suffix_array;

pub use config::Config;
pub use error::{ErrorKind, FmError, Result};
pub use service::IndexService;
