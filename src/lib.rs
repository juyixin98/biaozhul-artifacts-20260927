//! 有限标号迁移系统（LTS）之间的**弱迹包含**检查库。
//!
//! 模块边界：
//! * [`error`]：模块间统一的数据/错误契约（四类错误码、HTTP 映射）；
//! * [`model`]：输入语言的线缆类型与索引化内部表示；
//! * [`input`]：输入解析、一致性校验、可观察字母表对齐；
//! * [`solver`]：求解内核——静默闭包（含可回放映射）、弱像、子集对 BFS；
//! * [`evidence`]：独立证据验证——用自己的模拟重放反例迹并审计具体路径；
//! * [`service`]：Axum 后端接口与带运行编号的诊断日志。

pub mod error;
pub mod evidence;
pub mod input;
pub mod model;
pub mod service;
pub mod solver;
