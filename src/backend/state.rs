//! 共享应用状态：管理器注册表与边令牌编解码。
//!
//! 后端用不透明字符串令牌引用内核边，格式为
//! `m<manager-u64>-e<edge-slot-u32-decimal>`。令牌中的管理器 id 与管理器表
//! 双重校验：不存在的管理器或属于他管理器的边一律拒绝（后端侧的跨管理器
//! 混用防线，与内核入口校验互为纵深）。

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use crate::core::{BddError, BddManager, Edge, ManagerId, COMPLEMENT_BIT};

/// 管理器槽位：管理器本体与创建序号（对外展示用）。
pub struct ManagerSlot {
    pub manager: BddManager,
    pub seq: u64,
}

/// 应用状态（`Arc` 内部可变，便于 Axum 克隆）。
#[derive(Clone)]
pub struct AppState {
    inner: Arc<Mutex<StateInner>>,
}

struct StateInner {
    managers: HashMap<u64, ManagerSlot>,
    next_seq: u64,
}

impl AppState {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(Mutex::new(StateInner {
                managers: HashMap::new(),
                next_seq: 1,
            })),
        }
    }

    /// 创建并注册一个新管理器。
    pub fn create_manager(
        &self,
        variable_order: Vec<String>,
    ) -> Result<(u64, ManagerId), BddError> {
        let manager = BddManager::new(&variable_order)?;
        let mid = manager.id();
        let mut g = self.inner.lock().expect("state lock poisoned");
        let seq = g.next_seq;
        g.next_seq += 1;
        g.managers.insert(mid.0, ManagerSlot { manager, seq });
        Ok((seq, mid))
    }

    /// 在对管理器表加锁执行闭包。闭包返回 `Result<T, BddError>`，
    /// 锁中毒属于编程错误，直接 panic（与 std Mutex 约定一致）。
    pub fn with_manager<T>(
        &self,
        manager_id: u64,
        f: impl FnOnce(&mut BddManager) -> Result<T, BddError>,
    ) -> Result<T, BddError> {
        let mut g = self.inner.lock().expect("state lock poisoned");
        let slot = g
            .managers
            .get_mut(&manager_id)
            .ok_or(BddError::UnknownManager(manager_id))?;
        f(&mut slot.manager)
    }

    pub fn manager_count(&self) -> usize {
        self.inner
            .lock()
            .expect("state lock poisoned")
            .managers
            .len()
    }

    pub fn delete_manager(&self, manager_id: u64) -> bool {
        self.inner
            .lock()
            .expect("state lock poisoned")
            .managers
            .remove(&manager_id)
            .is_some()
    }
}

impl Default for AppState {
    fn default() -> Self {
        Self::new()
    }
}

/// 边令牌前缀。
pub const EDGE_TOKEN_PREFIX: &str = "m";

/// 编码边令牌。
pub fn encode_edge(edge: Edge) -> String {
    format!(
        "{}{}-e{}",
        EDGE_TOKEN_PREFIX,
        edge.manager_id().0,
        edge.slot()
    )
}

/// 解析边令牌并确认管理器存在；不校验边的具体存活状态（留给内核操作）。
pub fn decode_edge_token(state: &AppState, token: &str) -> Result<Edge, BddError> {
    let rest = token
        .strip_prefix(EDGE_TOKEN_PREFIX)
        .ok_or_else(|| BddError::MalformedEdgeToken(token.to_string()))?;
    let (mid_str, slot_str) = rest
        .split_once("-e")
        .ok_or_else(|| BddError::MalformedEdgeToken(token.to_string()))?;
    let manager_u64: u64 = mid_str
        .parse()
        .map_err(|_| BddError::MalformedEdgeToken(token.to_string()))?;
    let slot: u32 = slot_str
        .parse()
        .map_err(|_| BddError::MalformedEdgeToken(token.to_string()))?;

    // 管理器存在性检查（后端防线）。
    state.with_manager(manager_u64, |m| {
        let edge = Edge::new(ManagerId(manager_u64), slot);
        edge.require_owner(m.id())?;
        // 指向已回收/越界槽位也在此处暴露为 ReclaimedNode（终节点槽位合法）。
        let idx = slot & !COMPLEMENT_BIT;
        if idx as usize > m.allocated_slots() {
            return Err(BddError::ReclaimedNode {
                manager: manager_u64,
                index: idx,
            });
        }
        Ok(edge)
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edge_token_roundtrip_and_foreign_manager_rejection() {
        let state = AppState::new();
        let (_, mid_a) = state.create_manager(vec!["a".into()]).unwrap();
        let (_, mid_b) = state.create_manager(vec!["a".into()]).unwrap();

        let edge = state
            .with_manager(mid_a.0, |m| Ok(m.constant(true)))
            .unwrap();
        let token = encode_edge(edge);
        assert!(token.starts_with(&format!("m{}-e", mid_a.0)));
        let back = decode_edge_token(&state, &token).unwrap();
        assert_eq!(back, edge);

        // 伪造一个属于不存在管理器的令牌。
        let forged = "m9999-e0".to_string();
        assert!(matches!(
            decode_edge_token(&state, &forged),
            Err(BddError::UnknownManager(9999))
        ));
        assert!(matches!(
            decode_edge_token(&state, "garbage"),
            Err(BddError::MalformedEdgeToken(_))
        ));

        // 管理器 B 与 A 互不相干。
        assert_ne!(mid_a, mid_b);
    }
}
