//! ROBDD 布尔函数后端（库 crate）。
//!
//! 模块职责边界：
//! - [`lang`]：输入语言（JSON/文本表达式 AST、解析器、独立递归解释器）；
//! - [`core`]：求解内核（固定变量序 ROBDD、唯一表、补集边、apply、restrict、gc）；
//! - [`verify`]：证据验证（独立真值表预言机、变量身份映射、内核/预言机交叉比对）；
//! - [`backend`]：Axum HTTP 接口（状态管理、请求标识、脱敏诊断、路由装配）。

pub mod backend;
pub mod core;
pub mod lang;
pub mod verify;

pub use backend::app::build_router;
pub use backend::config::Config;
pub use backend::state::AppState;
