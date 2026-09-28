//! 垃圾回收：以显式注册的命名根为存活集合，标记-清扫（mark-sweep）。
//!
//! - **保留所有根引用**：从每个根边出发沿 `low/high` 可达的物理节点一律存活；
//! - 回收后不压缩槽位（边索引保持稳定），失效边在下次使用时以
//!   [`BddError::ReclaimedNode`] 被拒绝；
//! - 唯一表按存活节点重建，`apply` 记忆化缓存整体作废（其中可能含失效槽位）。

use super::edge::TERMINAL;
use super::{BddManager, Edge};
use serde::Serialize;

/// 一次回收的统计报告（作为诊断证据）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct GcReport {
    /// 回收前存活非终节点数。
    pub before_nodes: usize,
    /// 回收后存活非终节点数。
    pub after_nodes: usize,
    /// 本次标记为存活（含终节点）的物理节点数。
    pub marked: usize,
    /// 清扫掉的非终节点数。
    pub swept: usize,
    /// 作为存活根参与标记的根数量。
    pub roots: usize,
}

impl BddManager {
    /// 在“不回收”的前提下收集某条边可达的物理节点索引（用于重建后比对）。
    pub(crate) fn reachable_indices(&self, edge: Edge) -> Vec<u32> {
        let mut seen = vec![false; self.slots.len()];
        let mut stack = vec![edge];
        while let Some(e) = stack.pop() {
            let idx = e.index();
            if seen[idx as usize] {
                continue;
            }
            seen[idx as usize] = true;
            if idx != TERMINAL {
                let n = &self.slots[idx as usize].node;
                stack.push(n.low);
                stack.push(n.high);
            }
        }
        seen.iter()
            .enumerate()
            .filter(|(_, v)| **v)
            .map(|(i, _)| i as u32)
            .collect()
    }

    /// 执行垃圾回收。
    pub fn gc(&mut self) -> GcReport {
        let before = self.live_node_count();

        // 1) 标记：从全部根边出发 DFS。
        let mut live = vec![false; self.slots.len()];
        live[TERMINAL as usize] = true;
        let mut stack: Vec<Edge> = self.roots.values().copied().collect();
        while let Some(e) = stack.pop() {
            let idx = e.index();
            if live[idx as usize] {
                continue;
            }
            live[idx as usize] = true;
            if idx != TERMINAL {
                let n = &self.slots[idx as usize].node;
                stack.push(n.low);
                stack.push(n.high);
            }
        }
        let marked = live.iter().filter(|v| **v).count();

        // 2) 清扫：未标记的非终节点置为失效。
        let mut swept = 0usize;
        for (i, slot) in self.slots.iter_mut().enumerate() {
            if i == TERMINAL as usize {
                continue;
            }
            if !live[i] && slot.alive {
                slot.alive = false;
                swept += 1;
            }
        }

        // 3) 唯一表按存活节点重建（键恒为规范形态：节点 low 无补集）。
        self.unique.clear();
        for (i, slot) in self.slots.iter().enumerate() {
            if i == TERMINAL as usize || !slot.alive {
                continue;
            }
            let n = &slot.node;
            debug_assert!(!n.low.is_complemented());
            self.unique
                .insert((n.var.0, n.low.slot(), n.high.slot()), i as u32);
        }

        // 4) apply 缓存可能引用已失效槽位，整体作废。
        self.apply_cache.clear();

        let after = self.live_node_count();
        GcReport {
            before_nodes: before,
            after_nodes: after,
            marked,
            swept,
            roots: self.roots.len(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::BddError;
    use crate::lang::parser::parse;

    fn mgr() -> BddManager {
        BddManager::new(&["a".into(), "b".into(), "c".into()]).unwrap()
    }

    #[test]
    fn gc_keeps_roots_and_reclaims_unreachable_nodes() {
        let mut m = mgr();
        let keep = m.build(&parse("a & b").unwrap()).unwrap();
        let _garbage = m.build(&parse("a ^ b ^ c").unwrap()).unwrap();
        let before = m.live_node_count();
        assert!(before >= 3);

        m.add_root("keep", keep).unwrap();
        let report = m.gc();
        assert_eq!(report.roots, 1);
        assert_eq!(report.before_nodes, before);
        assert_eq!(report.after_nodes, 2); // a & b 的链：2 个非终节点
        assert!(report.swept >= 1);

        // 根仍然可用，结果正确。
        let r = m.root("keep").unwrap();
        assert!(m.evaluate(r, &[true, true, false]).unwrap());
        assert!(!(m.evaluate(r, &[true, false, true]).unwrap()));
    }

    #[test]
    fn reclaimed_edge_is_rejected_on_use() {
        let mut m = mgr();
        let g = m.build(&parse("a ^ b ^ c").unwrap()).unwrap();
        // 不注册任何根，全部非终节点可回收。
        let report = m.gc();
        assert_eq!(report.after_nodes, 0);
        let err = m.evaluate(g, &[true, true, true]);
        assert!(matches!(err, Err(BddError::ReclaimedNode { index, .. }) if index == g.index()));
    }

    #[test]
    fn rebuild_after_gc_yields_same_canonical_structure() {
        let mut m = mgr();
        let f1 = m.build(&parse("(a | b) & c").unwrap()).unwrap();
        m.add_root("f", f1).unwrap();
        // 制造垃圾后回收。
        let _ = m.build(&parse("a ^ b").unwrap()).unwrap();
        m.gc();

        // 用仍存活的根重建同一表达式，应得到同一条规范边（唯一表已重建）。
        let f2 = m.build(&parse("(a | b) & c").unwrap()).unwrap();
        assert_eq!(f1, f2);
        // 等价的另一语法同样落到同一边。
        let f3 = m.build(&parse("c & (b | a)").unwrap()).unwrap();
        assert_eq!(f1, f3);
    }
}
