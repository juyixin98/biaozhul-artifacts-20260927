//! 求解内核：ROBDD [`BddManager`]。
//!
//! 关键性质（由 [`manager`](self) 与 [`apply`]/[`restrict`]/[`gc`] 共同保证）：
//! - **固定变量序**：变量在管理器创建时一次性给定（索引小者在前），
//!   非终节点严格按序分流，`apply`/`restrict` 产出的边仍服从该序。
//! - **唯一表 + 冗余消除**：[`BddManager::mk`] 是唯一的建节点入口，
//!   相同 `(变量, 低边, 高边)` 复用同一节点，高低分支相同的节点不建立。
//! - **统一否定**：否定只经由补集位 [`Edge::flip`]，无独立“否定节点”。
//! - **管理器隔离**：每条边携带 [`ManagerId`]，入口处校验归属。
//!
//! 模块划分：
//! - [`edge`]：补集边与终节点约定；
//! - [`apply`]：二元 Shannon 递归（带记忆化）；
//! - [`restrict`]：变量限制（余因子）；
//! - [`gc`]：以显式根为存活集的标记-清扫回收。

pub mod apply;
pub mod edge;
pub mod gc;
pub mod restrict;

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

pub use edge::{Edge, ManagerId, COMPLEMENT_BIT, TERMINAL};

/// 变量标识（在管理器变量序中的下标，0 起）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct VarId(pub u32);

/// 内核错误。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BddError {
    /// 表达式引用了未在变量序中声明的变量。
    UnknownVariable(String),
    /// 变量序声明中出现重复变量。
    DuplicateVariable(String),
    /// 边属于另一个管理器，不能用于本管理器的运算。
    ForeignManager {
        edge_manager: u64,
        expected_manager: u64,
    },
    /// 边指向的节点已被上一轮回收（该边不再有效）。
    ReclaimedNode { manager: u64, index: u32 },
    /// 要求“同一管理器内”的两条边来自不同管理器。
    NotInSameManager { a: u64, b: u64 },
    /// 穷举真值表需要的变量数超过安全上限。
    TruthTableTooLarge { variables: usize, limit: usize },
    /// 身份映射不是双射或存在未绑定变量（等价请求被拒绝，非函数不等价）。
    MappingRejected(String),
    /// 引用了不存在的管理器（后端注册表中无此 id）。
    UnknownManager(u64),
    /// 边令牌格式非法。
    MalformedEdgeToken(String),
    /// 文本表达式解析失败（携带位置与原因）。
    ParseFailed { pos: usize, message: String },
}

impl std::fmt::Display for BddError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BddError::UnknownVariable(v) => write!(f, "unknown variable {v:?} (not in the declared variable order)"),
            BddError::DuplicateVariable(v) => write!(f, "duplicate variable {v:?} in variable order"),
            BddError::ForeignManager { edge_manager, expected_manager } => write!(
                f,
                "edge belongs to manager {edge_manager} but was used with manager {expected_manager}"
            ),
            BddError::ReclaimedNode { manager, index } => {
                write!(f, "node {index} in manager {manager} was reclaimed by garbage collection")
            }
            BddError::NotInSameManager { a, b } => {
                write!(f, "edges come from different managers ({a} vs {b})")
            }
            BddError::TruthTableTooLarge { variables, limit } => write!(
                f,
                "exhaustive truth table needs {variables} variables (> limit {limit})"
            ),
            BddError::MappingRejected(reason) => write!(f, "identity mapping rejected: {reason}"),
            BddError::UnknownManager(id) => write!(f, "manager {id} does not exist"),
            BddError::MalformedEdgeToken(tok) => write!(f, "malformed edge token {tok:?}"),
            BddError::ParseFailed { pos, message } => {
                write!(f, "parse error at {pos}: {message}")
            }
        }
    }
}

impl std::error::Error for BddError {}

static NEXT_MANAGER_ID: AtomicU64 = AtomicU64::new(1);

/// 非终节点。
#[derive(Debug, Clone)]
pub(crate) struct Node {
    var: VarId,
    /// 0-分支（存于唯一表时保证为无补集形式）。
    low: Edge,
    /// 1-分支。
    high: Edge,
}

#[derive(Debug, Clone)]
struct Slot {
    node: Node,
    alive: bool,
}

/// 唯一表键：(变量序号, 低边槽位, 高边槽位)。
/// 槽位包含补集位，因此补集/非补集形态能区分；但存入的键恒为规范形态
/// （低边无补集）。
type UniqueKey = (u32, u32, u32);

/// ROBDD 管理器。
pub struct BddManager {
    id: ManagerId,
    vars: Vec<String>,
    var_index: HashMap<String, VarId>,
    /// 0 号位是终节点占位（不会被读取字段）。
    slots: Vec<Slot>,
    /// 规范键 → 存活节点索引。
    unique: HashMap<UniqueKey, u32>,
    /// apply 记忆化缓存，键与值均为本管理器边的原始槽位。
    pub(crate) apply_cache: HashMap<(u8, u32, u32), Edge>,
    /// 显式根：回收时保证其可达子图存活。
    roots: HashMap<String, Edge>,
}

impl BddManager {
    /// 以固定变量序创建管理器。变量序索引即 [`VarId`]。
    pub fn new(variable_order: &[String]) -> Result<Self, BddError> {
        let mut var_index = HashMap::new();
        for (i, name) in variable_order.iter().enumerate() {
            if var_index.insert(name.clone(), VarId(i as u32)).is_some() {
                return Err(BddError::DuplicateVariable(name.clone()));
            }
        }
        let id = ManagerId(NEXT_MANAGER_ID.fetch_add(1, Ordering::Relaxed));
        let terminal = Slot {
            node: Node {
                var: VarId(u32::MAX),
                low: Edge::constant(id, true),
                high: Edge::constant(id, true),
            },
            alive: true,
        };
        Ok(Self {
            id,
            vars: variable_order.to_vec(),
            var_index,
            slots: vec![terminal],
            unique: HashMap::new(),
            apply_cache: HashMap::new(),
            roots: HashMap::new(),
        })
    }

    pub fn id(&self) -> ManagerId {
        self.id
    }

    /// 变量序（按序）。
    pub fn variable_order(&self) -> &[String] {
        &self.vars
    }

    pub fn var_id(&self, name: &str) -> Option<VarId> {
        self.var_index.get(name).copied()
    }

    /// 常量边。
    pub fn constant(&self, value: bool) -> Edge {
        Edge::constant(self.id, value)
    }

    /// 存活校验并返回节点；终节点直接返回 `None` 由调用方处理。
    pub(crate) fn node_at(&self, edge: Edge) -> Result<&Node, BddError> {
        edge.require_owner(self.id)?;
        let idx = edge.index();
        if idx == TERMINAL {
            return Err(BddError::ReclaimedNode {
                manager: self.id.0,
                index: idx,
            });
        }
        let slot = self
            .slots
            .get(idx as usize)
            .ok_or(BddError::ReclaimedNode {
                manager: self.id.0,
                index: idx,
            })?;
        if !slot.alive {
            return Err(BddError::ReclaimedNode {
                manager: self.id.0,
                index: idx,
            });
        }
        Ok(&slot.node)
    }

    /// 顶变量（诊断与校验使用）；终节点返回 `None`。
    pub fn top_var(&self, edge: Edge) -> Result<Option<VarId>, BddError> {
        if edge.as_value().is_some() {
            return Ok(None);
        }
        Ok(Some(self.node_at(edge)?.var))
    }

    fn push_node(&mut self, node: Node) -> u32 {
        let idx = self.slots.len() as u32;
        self.slots.push(Slot { node, alive: true });
        idx
    }

    /// 唯一的建节点入口：冗余消除 + 唯一表规约 + 低边规范化。
    ///
    /// 两条边必须属于本管理器（通常由递归内部调用保证）。
    pub(crate) fn mk(&mut self, var: VarId, low: Edge, high: Edge) -> Edge {
        debug_assert_eq!(low.manager_id(), self.id);
        debug_assert_eq!(high.manager_id(), self.id);

        // 冗余节点：两个分支相同则消除。
        if low == high {
            return low;
        }

        // 规范化：唯一表中低边恒为无补集；低边带补集时翻转两条边并翻转结果。
        let (key_low, key_high, flip_result) = if low.is_complemented() {
            (low.flip(), high.flip(), true)
        } else {
            (low, high, false)
        };

        let key = (var.0, key_low.slot(), key_high.slot());
        if let Some(&idx) = self.unique.get(&key) {
            let e = Edge::new(self.id, idx);
            return if flip_result { e.flip() } else { e };
        }

        let idx = self.push_node(Node {
            var,
            low: key_low,
            high: key_high,
        });
        self.unique.insert(key, idx);
        let e = Edge::new(self.id, idx);
        if flip_result {
            e.flip()
        } else {
            e
        }
    }

    /// 变量文字的边：`var=0 -> false`，`var=1 -> true`。
    pub fn var_edge(&mut self, var: VarId) -> Edge {
        let f = self.constant(false);
        let t = self.constant(true);
        self.mk(var, f, t)
    }

    /// 从输入语言 AST 构建 ROBDD。
    pub fn build(&mut self, expr: &crate::lang::Expr) -> Result<Edge, BddError> {
        match expr {
            crate::lang::Expr::Const(v) => Ok(self.constant(*v)),
            crate::lang::Expr::Var(name) => {
                let var = self
                    .var_index
                    .get(name)
                    .ok_or_else(|| BddError::UnknownVariable(name.clone()))?;
                Ok(self.var_edge(*var))
            }
            crate::lang::Expr::Not(e) => Ok(self.build(e)?.flip()),
            crate::lang::Expr::And(es) => {
                let mut acc = self.constant(true);
                for e in es {
                    let edge = self.build(e)?;
                    acc = self.apply(apply::Op::And, acc, edge)?;
                }
                Ok(acc)
            }
            crate::lang::Expr::Or(es) => {
                let mut acc = self.constant(false);
                for e in es {
                    let edge = self.build(e)?;
                    acc = self.apply(apply::Op::Or, acc, edge)?;
                }
                Ok(acc)
            }
            crate::lang::Expr::Xor(es) => {
                let mut acc = self.constant(false);
                for e in es {
                    let edge = self.build(e)?;
                    acc = self.apply(apply::Op::Xor, acc, edge)?;
                }
                Ok(acc)
            }
            crate::lang::Expr::Implies(a, b) => {
                let x = self.build(a)?;
                let y = self.build(b)?;
                self.apply(apply::Op::Implies, x, y)
            }
            crate::lang::Expr::Iff(a, b) => {
                let x = self.build(a)?;
                let y = self.build(b)?;
                self.apply(apply::Op::Iff, x, y)
            }
        }
    }

    /// 注册/覆盖命名根。回收时根的可达子图一定保留。
    pub fn add_root(&mut self, name: &str, edge: Edge) -> Result<(), BddError> {
        edge.require_owner(self.id)?;
        self.roots.insert(name.to_string(), edge);
        Ok(())
    }

    pub fn remove_root(&mut self, name: &str) -> Option<Edge> {
        self.roots.remove(name)
    }

    pub fn root(&self, name: &str) -> Option<Edge> {
        self.roots.get(name).copied()
    }

    pub fn root_names(&self) -> Vec<String> {
        self.roots.keys().cloned().collect()
    }

    /// 按变量序位置取值求值。`assignment[var.0 as usize]` 为该变量取值。
    pub fn evaluate(&self, edge: Edge, assignment: &[bool]) -> Result<bool, BddError> {
        edge.require_owner(self.id)?;
        let mut cur = edge;
        let mut parity = cur.is_complemented();
        loop {
            if cur.index() == TERMINAL {
                return Ok(!parity);
            }
            let node = self.node_at(cur)?;
            let bit =
                assignment
                    .get(node.var.0 as usize)
                    .copied()
                    .ok_or(BddError::UnknownVariable(
                        self.vars[node.var.0 as usize].clone(),
                    ))?;
            cur = if bit { node.high } else { node.low };
            if cur.is_complemented() {
                parity = !parity;
            }
        }
    }

    /// 存活的非终节点数量（终节点不计）。
    ///
    /// 注意：这是唯一表中全部未回收槽位，包含“从当前根不可达但尚未 GC”
    /// 的中间节点。要衡量某条边真正的 ROBDD 规模，用
    /// [`BddManager::reachable_node_count`]。
    pub fn live_node_count(&self) -> usize {
        self.slots.iter().filter(|s| s.alive).count() - 1
    }

    /// 从指定边可达的非终节点数量（该边真正的 ROBDD 规模）。
    pub fn reachable_node_count(&self, edge: Edge) -> Result<usize, BddError> {
        edge.require_owner(self.id)?;
        Ok(self
            .reachable_indices(edge)
            .into_iter()
            .filter(|&i| i != TERMINAL)
            .count())
    }

    /// 已分配槽位（含已回收空洞），诊断用。
    pub fn allocated_slots(&self) -> usize {
        self.slots.len() - 1
    }

    /// 当前根数量。
    pub fn root_count(&self) -> usize {
        self.roots.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang::parser::parse;

    fn mgr(vars: &[&str]) -> BddManager {
        BddManager::new(&vars.iter().map(|s| s.to_string()).collect::<Vec<_>>()).unwrap()
    }

    #[test]
    fn duplicate_variable_order_rejected() {
        let err = match BddManager::new(&["a".into(), "a".into()]) {
            Err(e) => e,
            Ok(_) => panic!("expected duplicate variable error"),
        };
        assert_eq!(err, BddError::DuplicateVariable("a".into()));
    }

    #[test]
    fn single_variable_function_has_one_node() {
        let mut m = mgr(&["a"]);
        let a = m.var_edge(VarId(0));
        assert_eq!(m.live_node_count(), 1);
        assert_eq!(a, a); // 同变量唯一
        assert!(!(m.evaluate(a, &[false]).unwrap()));
        assert!(m.evaluate(a, &[true]).unwrap());
        assert!(m.evaluate(a.flip(), &[false]).unwrap());
    }

    #[test]
    fn and_chain_collapses_to_n_nodes_and_evaluates_like_interpreter() {
        let mut m = mgr(&["a", "b", "c"]);
        let expr = parse("a & b & c").unwrap();
        let edge = m.build(&expr).unwrap();
        // 从根可达的 ROBDD 恰有 3 个节点；唯一表中还留着中间文字节点。
        assert_eq!(m.reachable_node_count(edge).unwrap(), 3);
        assert!(m.live_node_count() >= 3);
        for bits in 0..8u32 {
            let env = std::collections::HashMap::from([
                ("a".to_string(), bits & 1 != 0),
                ("b".to_string(), bits & 2 != 0),
                ("c".to_string(), bits & 4 != 0),
            ]);
            let assignment = [bits & 1 != 0, bits & 2 != 0, bits & 4 != 0];
            assert_eq!(
                m.evaluate(edge, &assignment).unwrap(),
                expr.eval(&env),
                "bits={bits:03b}"
            );
        }
    }

    #[test]
    fn xor_parity_has_n_nodes_with_complement_edges() {
        let mut m = mgr(&["a", "b", "c"]);
        let edge = m.build(&parse("a ^ b ^ c").unwrap()).unwrap();
        assert_eq!(m.reachable_node_count(edge).unwrap(), 3);
        // 其否定（同或）不增加可达节点。
        assert_eq!(m.reachable_node_count(edge.flip()).unwrap(), 3);
    }
}
