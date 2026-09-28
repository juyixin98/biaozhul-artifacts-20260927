//! 补集边（complemented edge）表示。
//!
//! ROBDD 的否定语义统一由边上的补集标记承担：
//! - 图中只有 **一个** 终节点（索引 0，表示“真”）；常量“假”用带补集标记的
//!   常量边表示。
//! - 每个非终节点的低分支（0-分支）永远以无补集形式存于唯一表中，
//!   从而保证结构唯一；`flip()` 是得到语义否定的唯一途径。
//!
//! `Edge` 还携带所属管理器的运行时标识 [`ManagerId`]，求解内核在每个入口
//! 校验边的归属，跨管理器的边无法被静默混用（见 [`BddError::ForeignManager`]）。

use crate::core::BddError;

/// 管理器实例标识（进程内单调递增）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct ManagerId(pub u64);

/// 节点索引。0 固定为唯一终节点 T。
pub type NodeIdx = u32;

/// 终节点索引。
pub const TERMINAL: NodeIdx = 0;

/// 补集标记位。
pub const COMPLEMENT_BIT: NodeIdx = 1 << 31;
pub(crate) const IDX_MASK: NodeIdx = !COMPLEMENT_BIT;

/// ROBDD 有向边：目标节点 + 补集标记 + 所属管理器。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct Edge {
    manager: ManagerId,
    slot: NodeIdx,
}

impl Edge {
    pub(crate) fn new(manager: ManagerId, slot: NodeIdx) -> Self {
        Self { manager, slot }
    }

    /// 所属管理器标识（用于后端生成作用域令牌）。
    pub fn manager_id(self) -> ManagerId {
        self.manager
    }

    /// 原始槽位（含补集标记位），仅供同管理器内部使用。
    pub(crate) fn slot(self) -> NodeIdx {
        self.slot
    }

    /// 目标节点索引（剥离补集标记）。
    pub(crate) fn index(self) -> NodeIdx {
        self.slot & IDX_MASK
    }

    /// 是否为补集边。
    pub fn is_complemented(self) -> bool {
        (self.slot & COMPLEMENT_BIT) != 0
    }

    /// 语义取反：翻转补集标记，不产生任何节点。
    #[must_use]
    pub fn flip(self) -> Self {
        Self {
            slot: self.slot ^ COMPLEMENT_BIT,
            ..self
        }
    }

    /// 常量边（带管理器归属）。
    pub(crate) fn constant(manager: ManagerId, value: bool) -> Self {
        Self {
            manager,
            slot: if value {
                TERMINAL
            } else {
                TERMINAL | COMPLEMENT_BIT
            },
        }
    }

    /// 若指向终节点，返回其布尔值。
    pub fn as_value(self) -> Option<bool> {
        (self.index() == TERMINAL).then(|| !self.is_complemented())
    }

    /// 入口守卫：所有接收外部边的内核方法先调用本函数。
    pub(crate) fn require_owner(self, expected: ManagerId) -> Result<(), BddError> {
        if self.manager == expected {
            Ok(())
        } else {
            Err(BddError::ForeignManager {
                edge_manager: self.manager.0,
                expected_manager: expected.0,
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constants_and_flip_roundtrip() {
        let m = ManagerId(7);
        let t = Edge::constant(m, true);
        let f = Edge::constant(m, false);
        assert_eq!(t.as_value(), Some(true));
        assert_eq!(f.as_value(), Some(false));
        assert_eq!(t.flip(), f);
        assert_eq!(f.flip().flip(), f);
        assert!(!t.is_complemented());
        assert!(f.is_complemented());
        assert_eq!(t.index(), TERMINAL);
        assert_eq!(f.index(), TERMINAL);
    }

    #[test]
    fn owner_guard_detects_foreign_edges() {
        let foreign = Edge::constant(ManagerId(1), true);
        assert!(foreign.require_owner(ManagerId(2)).is_err());
        assert!(foreign.require_owner(ManagerId(1)).is_ok());
    }
}
