//! `apply`：两个 ROBDD 的二元运算（Shannon 递归 + 记忆化）。
//!
//! 补集边规则（Andersen 第 5 章）：递归比较两节点顶变量，按序取最小变量分流；
//! 补集标记天然随边传播——因为 [`crate::core::BddManager::mk`] 规范化低边并翻转
//! 结果，调用方只需对常规 `low/high` 子边做递归。

use super::edge::Edge;
use super::{BddError, BddManager, VarId, TERMINAL};
use serde::{Deserialize, Serialize};

/// 支持的二元运算。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Op {
    /// 逻辑与
    And,
    /// 逻辑或
    Or,
    /// 异或
    Xor,
    /// 蕴含 a -> b
    Implies,
    /// 同或 / 双向蕴含
    Iff,
}

impl std::fmt::Display for Op {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Op::And => "and",
            Op::Or => "or",
            Op::Xor => "xor",
            Op::Implies => "implies",
            Op::Iff => "iff",
        })
    }
}

impl Op {
    fn code(self) -> u8 {
        match self {
            Op::And => 0,
            Op::Or => 1,
            Op::Xor => 2,
            Op::Implies => 3,
            Op::Iff => 4,
        }
    }

    /// 两个常量上的真值。
    pub fn eval_const(self, a: bool, b: bool) -> bool {
        match self {
            Op::And => a && b,
            Op::Or => a || b,
            Op::Xor => a ^ b,
            Op::Implies => !a || b,
            Op::Iff => a == b,
        }
    }
}

impl BddManager {
    /// 应用二元运算。两条边必须属于本管理器。
    pub fn apply(&mut self, op: Op, a: Edge, b: Edge) -> Result<Edge, BddError> {
        a.require_owner(self.id)?;
        b.require_owner(self.id)?;

        // 常量短路。
        match (a.as_value(), b.as_value()) {
            (Some(x), Some(y)) => return Ok(self.constant(op.eval_const(x, y))),
            (Some(x), None) => {
                if let Some(e) = self.single_terminal(op, x, true, b) {
                    return Ok(e);
                }
            }
            (None, Some(y)) => {
                if let Some(e) = self.single_terminal(op, y, false, a) {
                    return Ok(e);
                }
            }
            (None, None) => {}
        }

        let key = (op.code(), a.slot(), b.slot());
        if let Some(cached) = self.apply_cache.get(&key) {
            return Ok(*cached);
        }

        let va = if a.index() == TERMINAL {
            VarId(u32::MAX)
        } else {
            self.node_at(a)?.var
        };
        let vb = if b.index() == TERMINAL {
            VarId(u32::MAX)
        } else {
            self.node_at(b)?.var
        };

        // 分流变量 = 两侧最小顶变量。
        let var = va.min(vb);

        // 关键规则：进入边带补集标记时，它在分流变量上的两个分支都必须
        // 翻转——边 !e 在变量 v 上的余因子是 !e_v。不分支的一侧（顶变量
        // 更大或为常量）两个分支取自身。
        let (al0, ah0) = self.branches_at(a, va, var);
        let (bl0, bh0) = self.branches_at(b, vb, var);

        let low = self.apply(op, al0, bl0)?;
        let high = self.apply(op, ah0, bh0)?;
        let result = self.mk(var, low, high);
        self.apply_cache.insert(key, result);
        Ok(result)
    }

    /// 边在分流变量 `split` 上的 (低, 高) 分支。
    /// `top` 为该边的顶变量（常量传 `VarId(u32::MAX)`）。
    fn branches_at(&self, edge: Edge, top: VarId, split: VarId) -> (Edge, Edge) {
        if top != split {
            // 本层变量不在这条边上：两个分支都是它自身（补集位原样携带）。
            (edge, edge)
        } else {
            let n = self
                .node_at(edge)
                .expect("top == split implies a live non-terminal node");
            if edge.is_complemented() {
                // 进入边是补集边：!e 在 v 上的余因子是 !(e 在 v 上的余因子)，
                // 两个子分支都要翻转。
                (n.low.flip(), n.high.flip())
            } else {
                (n.low, n.high)
            }
        }
    }

    /// 恰好一边为常量 `x` 时的代数化简；`a_is_const` 指明常量是不是左操作数。
    fn single_terminal(&self, op: Op, x: bool, a_is_const: bool, other: Edge) -> Option<Edge> {
        let as_const = |v: bool| self.constant(v);
        let _ = a_is_const; // 下列规则在左右两种情形下相同（Iff/Xor 交换，已核对）
        match op {
            // x & t ≡ x?t:false；t & x 同理
            Op::And => Some(if x { other } else { as_const(false) }),
            // x | t ≡ x?true:t
            Op::Or => Some(if x { as_const(true) } else { other }),
            // x xor t ≡ x?!t:t
            Op::Xor => Some(if x { other.flip() } else { other }),
            // 仅“常量在左”时进入本分支：x -> t ≡ x?t:true
            Op::Implies => {
                if a_is_const {
                    Some(if x { other } else { as_const(true) })
                } else {
                    // t -> x ≡ x?true:!t
                    Some(if x { as_const(true) } else { other.flip() })
                }
            }
            // x iff t ≡ x?t:!t
            Op::Iff => Some(if x { other } else { other.flip() }),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang::parser::parse;

    fn mgr3() -> BddManager {
        BddManager::new(&["a".into(), "b".into(), "c".into()]).unwrap()
    }

    #[test]
    fn de_morgan_holds_via_canonical_equality() {
        let mut m = mgr3();
        let left = m.build(&parse("!(a & b)").unwrap()).unwrap();
        let right = m.build(&parse("!a | !b").unwrap()).unwrap();
        assert_eq!(left, right, "De Morgan: canonical edges must be identical");
        let left2 = m.build(&parse("!(a | b)").unwrap()).unwrap();
        let right2 = m.build(&parse("!a & !b").unwrap()).unwrap();
        assert_eq!(left2, right2);
    }

    #[test]
    fn iff_equals_negated_xor_without_extra_nodes() {
        let mut m = mgr3();
        let iff_e = m.build(&parse("a <-> b").unwrap()).unwrap();
        let xor_e = m.build(&parse("a ^ b").unwrap()).unwrap();
        assert_eq!(iff_e, xor_e.flip());
    }

    #[test]
    fn apply_checks_ownership() {
        let mut m1 = mgr3();
        let m2 = mgr3();
        let foreign = m2.constant(true);
        let err = m1.apply(Op::And, m1.constant(true), foreign).unwrap_err();
        assert!(matches!(err, BddError::ForeignManager { .. }));
        let _ = TERMINAL;
    }

    #[test]
    fn terminal_shortcuts_do_not_build_nodes() {
        let mut m = mgr3();
        let a = m.build(&parse("a").unwrap()).unwrap();
        let before = m.live_node_count();
        let r = m.apply(Op::And, a, m.constant(false)).unwrap();
        assert_eq!(r.as_value(), Some(false));
        assert_eq!(m.live_node_count(), before);
        let r = m.apply(Op::Xor, a, m.constant(true)).unwrap();
        assert_eq!(r, a.flip());
        let r = m.apply(Op::Implies, m.constant(false), a).unwrap();
        assert_eq!(r.as_value(), Some(true));
        let r = m.apply(Op::Implies, a, m.constant(true)).unwrap();
        assert_eq!(r.as_value(), Some(true));
        assert_eq!(m.live_node_count(), before);
    }
}
