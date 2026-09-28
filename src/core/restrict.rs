//! 变量限制（restrict / cofactor）：把指定变量固定为常量后得到的 ROBDD。
//!
//! 沿图递归，命中被限制变量的节点时直接选对应分支（补集位随边保留），
//! 经 [`BddManager::mk`] 重建，因此结果仍被唯一表规约。

use std::collections::HashMap;

use super::edge::Edge;
use super::{BddError, BddManager, VarId};

impl BddManager {
    /// 限制单个变量：`edge[var := value]`。
    ///
    /// 约定：递归以“无补集进入边”为记忆化单位——先把进入边的补集位移除，
    /// 记忆化表存规范结果，返回前再按需翻转。这保证同一物理节点无论从
    /// 补集边还是普通边到达，只递归一次。
    pub fn restrict_one(
        &mut self,
        edge: Edge,
        var: VarId,
        value: bool,
        memo: &mut HashMap<u32, Edge>,
    ) -> Result<Edge, BddError> {
        edge.require_owner(self.id)?;
        let entry_complement = edge.is_complemented();
        let canon_edge = if entry_complement { edge.flip() } else { edge };

        let result = if canon_edge.as_value().is_some() {
            canon_edge
        } else if let Some(hit) = memo.get(&canon_edge.slot()) {
            *hit
        } else {
            let node_var = self.node_at(canon_edge)?.var;
            let r = if node_var == var {
                // 命中被限制变量：直接选对应子边（子边自带补集位，原样保留）。
                let n = self.node_at(canon_edge)?;
                if value {
                    n.high
                } else {
                    n.low
                }
            } else if node_var < var {
                let (low, high) = {
                    let n = self.node_at(canon_edge)?;
                    (n.low, n.high)
                };
                let rl = self.restrict_one(low, var, value, memo)?;
                let rh = self.restrict_one(high, var, value, memo)?;
                self.mk(node_var, rl, rh)
            } else {
                // node_var > var：被限制变量不在本子图出现。
                canon_edge
            };
            memo.insert(canon_edge.slot(), r);
            r
        };

        Ok(if entry_complement {
            result.flip()
        } else {
            result
        })
    }

    /// 一次限制多个变量。按变量序升序应用，结果与给定映射的键顺序无关。
    pub fn restrict(
        &mut self,
        edge: Edge,
        values: &HashMap<String, bool>,
    ) -> Result<Edge, BddError> {
        edge.require_owner(self.id)?;
        // 把名称解析为 VarId（未知变量立即拒绝）。
        let mut assignments: Vec<(VarId, bool)> = Vec::new();
        for (name, value) in values {
            let var = self
                .var_id(name)
                .ok_or_else(|| BddError::UnknownVariable(name.clone()))?;
            assignments.push((var, *value));
        }
        assignments.sort_by_key(|(v, _)| *v);

        let mut current = edge;
        let mut memo = HashMap::new();
        for (var, value) in assignments {
            memo.clear();
            current = self.restrict_one(current, var, value, &mut memo)?;
        }
        Ok(current)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang::parser::parse;

    #[test]
    fn restrict_majority3_collapses_correctly() {
        // majority(a,b,c) = ab | ac | bc；a=1 → b|c；a=0 → bc
        let mut m = BddManager::new(&["a".into(), "b".into(), "c".into()]).unwrap();
        let f = m
            .build(&parse("(a & b) | (a & c) | (b & c)").unwrap())
            .unwrap();

        let a = VarId(0);
        let mut memo = HashMap::new();
        let f_a1 = m.restrict_one(f, a, true, &mut memo).unwrap();
        let b_or_c = m.build(&parse("b | c").unwrap()).unwrap();
        assert_eq!(f_a1, b_or_c);

        let mut memo = HashMap::new();
        let f_a0 = m.restrict_one(f, a, false, &mut memo).unwrap();
        let b_and_c = m.build(&parse("b & c").unwrap()).unwrap();
        assert_eq!(f_a0, b_and_c);
    }

    #[test]
    fn restrict_preserves_complement_and_matches_interpreter() {
        let mut m = BddManager::new(&["a".into(), "b".into()]).unwrap();
        let expr = parse("!(a & b)").unwrap();
        let f = m.build(&expr).unwrap();
        let g = m
            .restrict(f, &HashMap::from([("a".to_string(), true)]))
            .unwrap();
        // !(true & b) = !b
        let not_b = m.build(&parse("!b").unwrap()).unwrap();
        assert_eq!(g, not_b);

        let env_all = std::collections::HashMap::from([("b".to_string(), false)]);
        assert_eq!(
            m.evaluate(g, &[true, false]).unwrap(),
            expr.eval(&{
                let mut e = env_all.clone();
                e.insert("a".to_string(), true);
                e
            })
        );
    }

    #[test]
    fn restrict_unknown_variable_and_foreign_edge_rejected() {
        let mut m = BddManager::new(&["a".into()]).unwrap();
        let f = m.constant(true);
        assert_eq!(
            m.restrict(f, &HashMap::from([("zzz".to_string(), true)])),
            Err(BddError::UnknownVariable("zzz".into()))
        );
        let m2 = BddManager::new(&["a".into()]).unwrap();
        assert!(matches!(
            m.restrict(
                m2.constant(false),
                &HashMap::from([("a".to_string(), true)])
            ),
            Err(BddError::ForeignManager { .. })
        ));
    }
}
