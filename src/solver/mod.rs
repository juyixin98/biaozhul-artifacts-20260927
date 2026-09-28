//! 求解内核：把差约束翻译为带权图，用 Bellman-Ford 判定可行性、
//! 产出可行赋值或完全由命名约束构成的负环证据。
//!
//! 图模型（见 [`graph`]）：约束 `x - y <= c` 翻译为图边 `y -> x`，权 `c`。
//! 向所有节点连 0 权边的超源节点是**内核内部的匿名节点**，其边绝不出现在证据中；
//! 它同时保证不连通分量被同等对待（任意分量中的负环都能被发现）。
//!
//! 算法（见 [`bellman`]）：所有距离从 0 开始（等价于显式超源初始化），
//! 至多 n-1 轮松弛后再做一轮检测轮；仍可松弛 => 存在负环，沿前驱链取环。
//! 所有加法均为 checked 加法：溢出不是 UB/回绕，而是 [`SolverError::ArithmeticOverflow`]。

pub mod bellman;
pub mod graph;

pub use bellman::{solve_trace, TracePass};
pub use graph::{build_graph, Graph, SolverError};

/// 求解内核的结论（位置下标形式，变量名/约束名由外层映射）。
#[derive(Debug, Clone)]
pub enum KernelOutcome {
    /// 可行：给出每个变量（按下标）的整数赋值。
    Feasible {
        assignment: Vec<i64>,
        /// 各松弛轮的关键中间状态，用于重放日志。
        trace: Vec<TracePass>,
    },
    /// 不可行：负环（边下标序列，按行走顺序，闭合）及其严格负费用。
    Unsat {
        cycle_edges: Vec<usize>,
        total_cost: i64,
        detection_pass: usize,
    },
}
