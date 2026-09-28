//! 约束 -> 图边的构建与配额。

/// 变量数上限（影响 Bellman-Ford 的 O(V·E) 开销，超出返回资源耗尽）。
pub const MAX_VARIABLES: usize = 128;
/// 约束/边数上限。
pub const MAX_CONSTRAINTS: usize = 1024;

/// 内核错误：不携带 HTTP 语义，由外层映射为错误类别。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SolverError {
    /// 变量数超过 [`MAX_VARIABLES`]。
    TooManyVariables { limit: usize, got: usize },
    /// 约束数超过 [`MAX_CONSTRAINTS`]。
    TooManyConstraints { limit: usize, got: usize },
    /// 整数加法溢出（距离松弛或环费用求和时）。明确拒绝，不回绕。
    ArithmeticOverflow {
        /// 溢出发生的位置描述，便于重放定位。
        where_: String,
    },
    /// 求解器内部不变量被破坏（如取出的环不严格为负）。外层映射为 500。
    InvariantViolation(String),
}

/// 一条有向边。每条边都由一条命名约束产生；内核另有一个隐式超源节点，
/// 但其 0 权边不会被实例化为 [`Edge`]，因此永远不会进入证据。
#[derive(Debug, Clone, Copy)]
pub struct Edge {
    /// 源节点下标（约束 `x - y <= c` 中的 y）。
    pub from: usize,
    /// 目标节点下标（约束中的 x）。
    pub to: usize,
    /// 权 c。
    pub weight: i64,
    /// 产生该边的约束在输入序列中的下标；证据据此映射回约束 ID。
    pub source: usize,
}

/// 编译后的约束图：变量以 intern 顺序编号。
#[derive(Debug)]
pub struct Graph {
    /// 变量下标 -> 变量名。
    pub var_names: Vec<String>,
    /// 边（下标即证据中使用的边下标）。
    pub edges: Vec<Edge>,
}

impl Graph {
    pub fn variable_count(&self) -> usize {
        self.var_names.len()
    }

    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }
}

/// 从已通过输入校验的约束三元组 `(name, x, y, c)` 构建图。
///
/// `rows` 的下标即“约束下标”（也是 [`Edge::source`]）。
/// 在 intern 变量前先做配额检查，使资源耗尽优先于计算。
pub fn build_graph(rows: &[(String, String, String, i64)]) -> Result<Graph, SolverError> {
    if rows.len() > MAX_CONSTRAINTS {
        return Err(SolverError::TooManyConstraints {
            limit: MAX_CONSTRAINTS,
            got: rows.len(),
        });
    }

    // 先收集变量并判配额，再做 intern，避免把一半变量装进表才拒绝。
    let mut seen = std::collections::HashSet::new();
    for (_, x, y, _) in rows {
        seen.insert(x.as_str());
        seen.insert(y.as_str());
    }
    if seen.len() > MAX_VARIABLES {
        return Err(SolverError::TooManyVariables {
            limit: MAX_VARIABLES,
            got: seen.len(),
        });
    }

    let mut var_names: Vec<String> = Vec::with_capacity(seen.len());
    let mut index = std::collections::HashMap::<&str, usize>::with_capacity(seen.len());
    for (_, x, y, _) in rows {
        for v in [x, y] {
            if !index.contains_key(v.as_str()) {
                index.insert(v.as_str(), var_names.len());
                var_names.push(v.clone());
            }
        }
    }

    let mut edges = Vec::with_capacity(rows.len());
    for (source, (_, x, y, c)) in rows.iter().enumerate() {
        edges.push(Edge {
            from: index[y.as_str()],
            to: index[x.as_str()],
            weight: *c,
            source,
        });
    }
    Ok(Graph { var_names, edges })
}
