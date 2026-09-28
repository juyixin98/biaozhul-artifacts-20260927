//! 输入语言模块：把线缆 JSON 结构校验、索引化为 [`crate::model::AlignedSystem`]。
//!
//! 这里负责“可观察字母表明确对齐”：
//! * 全局 `observable_actions` 是唯一的可观察动作表（两侧共用、下标一致）；
//! * 任何边的动作要么在该表中，要么在其所属一侧的 `hidden_actions` 中；
//! * 重复声明、表重叠、悬空名字等一律作为 [`crate::error::ErrorCode`] 中的具体冲突返回。

use std::collections::HashMap;

use crate::error::{ApiError, ErrorCode, Result};
use crate::model::{
    AlignedSystem, CheckRequestWire, Edge, EdgeLabel, EdgeWire, LimitsWire, Lts, LtsWire, Side,
};
use crate::solver::SolverLimits;

/// 解析与对齐的总入口。
pub fn parse_request(req: CheckRequestWire) -> Result<(AlignedSystem, SolverLimits)> {
    let alphabet = check_alphabet(&req.observable_actions)?;
    let limits = resolve_limits(req.limits.as_ref())?;
    let specification = build_lts(req.specification, Side::Specification, &alphabet, &limits)?;
    let implementation =
        build_lts(req.implementation, Side::Implementation, &alphabet, &limits)?;
    Ok((
        AlignedSystem {
            alphabet,
            specification,
            implementation,
        },
        limits,
    ))
}

fn check_alphabet(raw: &[String]) -> Result<Vec<String>> {
    if raw.is_empty() {
        return Err(ApiError::new(
            ErrorCode::EmptyObservableAlphabet,
            "observable_actions 不能为空：必须显式声明可观察字母表",
        ));
    }
    let mut seen: HashMap<&str, ()> = HashMap::new();
    for a in raw {
        if a.is_empty() {
            return Err(ApiError::new(
                ErrorCode::InvalidActionName,
                "可观察动作名不能为空串",
            ));
        }
        if seen.contains_key(a.as_str()) {
            return Err(ApiError::new(
                ErrorCode::DuplicateObservableAction,
                format!("可观察动作 {a:?} 在 observable_actions 中重复声明"),
            ));
        }
        seen.insert(a, ());
    }
    Ok(raw.to_vec())
}

fn resolve_limits(wire: Option<&LimitsWire>) -> Result<SolverLimits> {
    let mut limits = SolverLimits::default();
    let Some(w) = wire else {
        return Ok(limits);
    };
    // 0 在语义上等于“不允许任何资源”，只会立刻耗尽；为避免歧义，要求正数。
    fn positive(value: Option<usize>, field: &str) -> Result<Option<usize>> {
        match value {
            Some(0) => Err(ApiError::new(
                ErrorCode::InvalidLimitValue,
                format!("limits.{field} 必须为正整数（收到 0）"),
            )),
            other => Ok(other),
        }
    }
    if let Some(v) = positive(w.max_states_per_lts, "max_states_per_lts")? {
        limits.max_states_per_lts = v;
    }
    if let Some(v) = positive(w.max_edges_per_lts, "max_edges_per_lts")? {
        limits.max_edges_per_lts = v;
    }
    if let Some(v) = positive(w.max_alphabet, "max_alphabet")? {
        limits.max_alphabet = v;
    }
    if let Some(v) = positive(w.max_closure_pairs, "max_closure_pairs")? {
        limits.max_closure_pairs = v;
    }
    if let Some(v) = positive(w.max_search_nodes, "max_search_nodes")? {
        limits.max_search_nodes = v;
    }
    Ok(limits.clamp_to_hard())
}

/// 名称表：边/初态中首次出现的状态按出现顺序补登。
struct NameTable {
    names: Vec<String>,
    index: HashMap<String, u32>,
}

impl NameTable {
    fn new() -> Self {
        Self {
            names: Vec::new(),
            index: HashMap::new(),
        }
    }

    fn declare(&mut self, name: &str) -> Result<u32> {
        if name.is_empty() {
            return Err(ApiError::new(
                ErrorCode::EmptyStateName,
                "状态名不能为空串",
            ));
        }
        if let Some(&idx) = self.index.get(name) {
            return Ok(idx);
        }
        let idx = self.names.len() as u32;
        self.names.push(name.to_owned());
        self.index.insert(name.to_owned(), idx);
        Ok(idx)
    }

    fn lookup(&self, name: &str) -> Option<u32> {
        self.index.get(name).copied()
    }
}

fn build_lts(
    wire: LtsWire,
    side: Side,
    alphabet: &[String],
    limits: &SolverLimits,
) -> Result<Lts> {
    let side_str = side.as_str();

    // 1) 字母表下标表与本侧隐藏动作表（重复/重叠立即报错）。
    let alphabet_index: HashMap<&str, u32> = alphabet
        .iter()
        .enumerate()
        .map(|(i, a)| (a.as_str(), i as u32))
        .collect();

    let mut hidden_set: HashMap<&str, ()> = HashMap::new();
    for h in &wire.hidden_actions {
        if h.is_empty() {
            return Err(ApiError::new(
                ErrorCode::InvalidActionName,
                format!("{side_str}.hidden_actions 中存在空动作名"),
            ));
        }
        if alphabet_index.contains_key(h.as_str()) {
            return Err(ApiError::new(
                ErrorCode::ObservableHiddenOverlap,
                format!("动作 {h:?} 同时被声明为可观察动作和 {side_str} 的静默动作"),
            ));
        }
        if hidden_set.contains_key(h.as_str()) {
            return Err(ApiError::new(
                ErrorCode::DuplicateHiddenAction,
                format!("静默动作 {h:?} 在 {side_str}.hidden_actions 中重复声明"),
            ));
        }
        hidden_set.insert(h.as_str(), ());
    }

    if alphabet.len() > limits.max_alphabet {
        return Err(ApiError::new(
            ErrorCode::HardLimitExceeded,
            format!(
                "可观察字母表大小 {} 超过硬上限 {}",
                alphabet.len(),
                limits.max_alphabet
            ),
        ));
    }

    // 2) 状态表：先登记显式声明（重复即冲突），边和初态中出现的新名字再补登。
    let mut names = NameTable::new();
    for s in &wire.states {
        if s.is_empty() {
            return Err(ApiError::new(
                ErrorCode::EmptyStateName,
                format!("{side_str}.states 中存在空状态名"),
            ));
        }
        if names.index.contains_key(s) {
            return Err(ApiError::new(
                ErrorCode::DuplicateState,
                format!("状态 {s:?} 在 {side_str}.states 中重复声明"),
            ));
        }
        names.declare(s)?;
    }

    if wire.initial_states.is_empty() {
        return Err(ApiError::new(
            ErrorCode::InitialStatesEmpty,
            format!("{side_str} 必须声明至少一个初始状态"),
        ));
    }
    let mut initial_states = Vec::with_capacity(wire.initial_states.len());
    for s in &wire.initial_states {
        if let Some(&idx) = names.index.get(s) {
            // 初态不允许重复：重复会静默改变语义，按状态冲突处理。
            if initial_states.contains(&idx) {
                return Err(ApiError::new(
                    ErrorCode::DuplicateState,
                    format!("初始状态 {s:?} 在 {side_str}.initial_states 中重复出现"),
                ));
            }
            initial_states.push(idx);
        } else {
            // 初态引用了未声明状态：按悬空引用处理，不自动补登。
            return Err(ApiError::new(
                ErrorCode::UnknownState,
                format!("{side_str}.initial_states 引用了未声明的状态 {s:?}"),
            ));
        }
    }

    if wire.edges.len() > limits.max_edges_per_lts {
        return Err(ApiError::new(
            ErrorCode::HardLimitExceeded,
            format!(
                "{side_str} 的边数 {} 超过上限 {}",
                wire.edges.len(),
                limits.max_edges_per_lts
            ),
        ));
    }

    // 3) 用户自定义边 id 唯一性检查。
    let mut user_edge_ids: HashMap<&str, ()> = HashMap::new();
    for e in &wire.edges {
        if let Some(id) = &e.id {
            if id.is_empty() {
                return Err(ApiError::new(
                    ErrorCode::EmptyEdgeId,
                    format!("{side_str} 存在 id 为空串的边"),
                ));
            }
            if user_edge_ids.contains_key(id.as_str()) {
                return Err(ApiError::new(
                    ErrorCode::DuplicateEdgeId,
                    format!("边 id {id:?} 在 {side_str} 中重复使用"),
                ));
            }
            user_edge_ids.insert(id.as_str(), ());
        }
    }

    // 4) 解析边：补登端点状态、动作对齐到全局字母表或本侧 τ。
    let mut raw_edges: Vec<(u32, u32, bool, u32, String, Option<String>)> =
        Vec::with_capacity(wire.edges.len());
    for EdgeWire {
        id,
        source,
        action,
        target,
    } in wire.edges
    {
        let src = match names.lookup(&source) {
            Some(i) => i,
            None => {
                // 先补登；若在声明表里其实不会走到这（lookup 已命中）。
                names.declare(&source).map_err(|_| {
                    ApiError::new(
                        ErrorCode::UnknownState,
                        format!("{side_str} 边引用了无效源状态 {source:?}"),
                    )
                })?
            }
        };
        let dst = names.declare(&target).map_err(|_| {
            ApiError::new(
                ErrorCode::UnknownState,
                format!("{side_str} 边引用了无效目标状态 {target:?}"),
            )
        })?;

        let label = if let Some(&action_idx) = alphabet_index.get(action.as_str()) {
            EdgeLabel::Observable {
                action_idx,
                name: action.clone(),
            }
        } else if hidden_set.contains_key(action.as_str()) {
            EdgeLabel::Tau {
                name: action.clone(),
            }
        } else {
            return Err(ApiError::new(
                ErrorCode::UnknownAction,
                format!(
                    "{side_str} 边 {source} -- {action} --> {target} 使用了既不在 observable_actions 也不在 {side_str}.hidden_actions 中的动作 {action:?}"
                ),
            ));
        };
        let (is_tau, action_order) = match &label {
            EdgeLabel::Observable { action_idx, .. } => (false, *action_idx),
            EdgeLabel::Tau { .. } => (true, u32::MAX),
        };
        raw_edges.push((src, dst, is_tau, action_order, action, id));
    }

    if names.names.len() > limits.max_states_per_lts {
        return Err(ApiError::new(
            ErrorCode::HardLimitExceeded,
            format!(
                "{side_str} 的状态数 {} 超过上限 {}",
                names.names.len(),
                limits.max_states_per_lts
            ),
        ));
    }

    // 5) 规范排序：(源, τ在前? 由动作类别决定) —— 采用 (source, kind: obs先按索引/tau, target, user_id, 出现序)。
    //    顺序固定后，边的数组下标即为可回放的规范边索引。
    let mut order: Vec<usize> = (0..raw_edges.len()).collect();
    order.sort_by(|&a, &b| {
        let ea = &raw_edges[a];
        let eb = &raw_edges[b];
        ea.0.cmp(&eb.0) // source
            .then(ea.2.cmp(&eb.2)) // obs(false) 在 tau(true) 前
            .then(ea.3.cmp(&eb.3)) // 动作顺序（observable 索引）
            .then(ea.1.cmp(&eb.1)) // target
            .then_with(|| ea.5.cmp(&eb.5))
    });

    let n = names.names.len();
    let mut edges = Vec::with_capacity(raw_edges.len());
    let mut out: Vec<Vec<u32>> = vec![Vec::new(); n];
    for (canon_index, &raw_idx) in order.iter().enumerate() {
        let (src, dst, is_tau, action_idx, action_name, user_id) =
          raw_edges[raw_idx].clone();
        let label = if is_tau {
            EdgeLabel::Tau {
                name: action_name,
            }
        } else {
            EdgeLabel::Observable {
                action_idx,
                name: action_name,
            }
        };
        let canon = canon_index as u32;
        out[src as usize].push(canon);
        edges.push(Edge {
            canon_index: canon,
            user_id,
            source: src,
            target: dst,
            label,
        });
    }

    // 6) 派生邻接表：obs_out[s][a] 与 tau_out[s]，去重后排序。
    let mut obs_out: Vec<Vec<Vec<u32>>> =
        vec![vec![Vec::new(); alphabet.len()]; n];
    let mut tau_out: Vec<Vec<u32>> = vec![Vec::new(); n];
    for e in &edges {
        match &e.label {
            EdgeLabel::Observable { action_idx, .. } => {
                obs_out[e.source as usize][*action_idx as usize].push(e.target);
            }
            EdgeLabel::Tau { .. } => tau_out[e.source as usize].push(e.target),
        }
    }
    for v in &mut obs_out {
        for targets in v {
            targets.sort_unstable();
            targets.dedup();
        }
    }
    for targets in &mut tau_out {
        targets.sort_unstable();
        targets.dedup();
    }

    Ok(Lts {
        name: wire.name,
        state_names: names.names,
        initial_states,
        edges,
        out,
        obs_out,
        tau_out,
        hidden_names: wire.hidden_actions,
    })
}
