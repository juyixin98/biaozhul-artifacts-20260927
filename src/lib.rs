//! DIMACS CNF 求解后端库。
//!
//! 模块职责：
//! - [`cnf`]：CNF 数据模型与子句规范化（去重/互补/空子句）。
//! - [`input`]：输入语言（DIMACS 文本、结构化 JSON）的解析。
//! - [`solver`]：求解内核（DPLL、双监视文字、确定性分支、预算控制、消解证明记录）。
//! - [`evidence`]：SAT/UNSAT 证据的序列化契约。
//! - [`verify`]：独立证据检查器（不导入求解器代码）。
//! - [`api`]：Axum HTTP 接口与诊断。
//! - [`config`]：环境变量配置。

pub mod api;
pub mod cnf;
pub mod config;
pub mod evidence;
pub mod input;
pub mod solver;
pub mod verify;
