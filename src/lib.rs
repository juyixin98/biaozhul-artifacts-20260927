//! 整数差约束（x - y <= c）集合服务。
//!
//! 模块边界（数据与错误契约见各模块文档）：
//! - [`model`]   输入语言：HTTP/内核共用的 DTO 与输入校验。
//! - [`error`]   错误分类：input_error / state_conflict / resource_exhausted /
//!   computation_failure / not_found / internal_error，贯穿所有层。
//! - [`solver`]  求解内核：约束 -> 图边 -> Bellman-Ford，产出可行赋值或负环证据。
//! - [`evidence`] 证据验证：独立复核“一组约束 ID 是否构成严格负环”。
//! - [`store`]   状态层：命名约束集合，批次原子提交，版本化快照。
//! - [`api`]     后端接口：axum 路由、run id、错误到 HTTP 的映射。

pub mod api;
pub mod error;
pub mod evidence;
pub mod model;
pub mod solver;
pub mod store;
