//! pr2d：离线预注册坐标的二维点增量 / 矩形和查询后端。
//!
//! 分层：
//! - [`model`] / [`error`]：数据模型与稳定错误分类
//! - [`coord`] / [`fenwick`] / [`rect`]：索引内核（坐标压缩、二维 BIT、矩形语义）
//! - [`persist`]：文件系统 WAL（CRC 校验、fsync、崩溃重放）
//! - [`store`]：版本化状态机（原子批发布、历史版本可读）
//! - [`api`]：Axum 验证接口
//! - [`config`] / [`telemetry`]：配置层与运行身份/日志

pub mod api;
pub mod config;
pub mod coord;
pub mod error;
pub mod fenwick;
pub mod model;
pub mod persist;
pub mod rect;
pub mod store;
pub mod telemetry;
