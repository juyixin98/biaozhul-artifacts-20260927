//! petri-reach：显式容量上界的普通带权 Petri 网标识可达性分析后端。
//!
//! 分层：
//! - [`input`]：输入语言（JSON、`.pnet` 文本）与语义校验；
//! - [`kernel`]：网模型、发射语义、可达性 BFS、P 不变量；
//! - [`verify`]：独立证据重放与候选验证；
//! - [`api`]：Axum HTTP 接口；
//! - [`config`]：TOML + 环境变量配置。
//!
//! 完备性边界：结论仅在容量模型内成立，不等价于无界 Petri 网的完整可达性判定。

pub mod api;
pub mod config;
pub mod input;
pub mod kernel;
pub mod request_id;
pub mod verify;

pub use crate::kernel::state_space_upper_bound;
