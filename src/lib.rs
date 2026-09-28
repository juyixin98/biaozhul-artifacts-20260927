//! cnf-dpll：DIMACS CNF 求解后端的库入口。
//!
//! 模块职责分层：
//! - [`lit`] / [`normalize`]：核心数据类型与子句规范化；
//! - [`input`]：DIMACS CNF 输入语言与解析错误分类；
//! - [`solver`]：DPLL + 双监视文字 + 确定性分支 + 学习证明的求解内核；
//! - [`evidence`]：SAT 模型 / UNSAT 归结证明的数据契约与**独立**检查器；
//! - [`diagnostics`]：请求标识、关键状态与脱敏；
//! - [`api`]：Axum HTTP 接口；
//! - [`config`]：环境变量配置。

pub mod api;
pub mod config;
pub mod diagnostics;
pub mod evidence;
pub mod input;
pub mod lit;
pub mod normalize;
pub mod solver;

pub use evidence::types::Outcome;
pub use input::{parse_and_normalize, ParseError, ParsedDimacs};
pub use solver::{solve_normalized, Budget, BudgetExceeded, SolveResult, Solver};
