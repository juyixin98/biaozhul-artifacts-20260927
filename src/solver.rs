//! 求解内核。
//!
//! 核心模型（τ 为声明的静默动作）：
//! * 静默闭包 `C(X) = { t | ∃s∈X, s ==τ⇒ t }`，连同每个二元组保留一条可回放的 τ 前驱边；
//! * 弱像 `Img_a(X) = C({ t | ∃s∈X, s --a--> t })`，即“先 a 后任意 τ”；
//! * 在状态**子集对** `(I, S)` 上做分层 BFS：I 是实现经可观察迹可达的闭包集合，
//!   S 是规格经同一条迹可达的闭包集合。两者都用精确的状态集合标识，
//!   **绝不**因为两个状态“单步动作看起来相同”就合并（那是模拟/商集做法，会漏报反例）。
//! * BFS 按层（迹长度）扩展，并按字母表索引顺序生成，找到的第一个 `I ≠ ∅, S = ∅`
//!   的子集对，给出的就是长度最短、同长度下字典序最小的反例迹。
//! * 任一类资源上限被突破都返回 `unknown`，与“成立”严格区分。

use serde::Serialize;

use crate::model::{AlignedSystem, EdgeLabel, Lts};

/// 资源限制。请求可下调，不能超过硬上限（防止一次请求耗尽服务）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SolverLimits {
    pub max_states_per_lts: usize,
    pub max_edges_per_lts: usize,
    pub max_alphabet: usize,
    pub max_closure_pairs: usize,
    pub max_search_nodes: usize,
}

impl Default for SolverLimits {
    fn default() -> Self {
        Self {
            max_states_per_lts: 500_000,
            max_edges_per_lts: 2_000_000,
            max_alphabet: 4_096,
            max_closure_pairs: 2_000_000,
            max_search_nodes: 100_000,
        }
    }
}

impl SolverLimits {
    /// 不允许把限制上调到硬天花板之上。
    #[must_use]
    pub fn clamp_to_hard(mut self) -> Self {
        let hard = Self::default();
        self.max_states_per_lts = self.max_states_per_lts.min(hard.max_states_per_lts);
        self.max_edges_per_lts = self.max_edges_per_lts.min(hard.max_edges_per_lts);
        self.max_alphabet = self.max_alphabet.min(hard.max_alphabet);
        self.max_closure_pairs = self.max_closure_pairs.min(hard.max_closure_pairs);
        self.max_search_nodes = self.max_search_nodes.min(hard.max_search_nodes);
        self
    }
}

/// 未知结论的具体原因（状态爆炸类），与成立/反例严格区分。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum UnknownReason {
    /// 静默闭包/弱像的二元组数量超预算。
    ClosurePairLimit,
    /// 子集对 BFS 访问节点数超预算。
    SearchNodeLimit,
}

impl UnknownReason {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            UnknownReason::ClosurePairLimit => "closure_pair_limit",
            UnknownReason::SearchNodeLimit => "search_node_limit",
        }
    }
}

/// 内核计算失败（不应发生；发生即 500，绝不伪装成成立/未知）。
#[derive(Debug)]
pub enum KernelError {
    ComputationFailed(String),
}

impl std::fmt::Display for KernelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KernelError::ComputationFailed(msg) => write!(f, "求解器计算失败: {msg}"),
        }
    }
}

/// 一个可达二元组 `(start, target)` 的回放记录：
/// target 是从 start 沿静默边被发现的，使用边 `edge`，前驱为 `parent`。
#[derive(Debug, Clone, Copy)]
pub struct ParentEntry {
    pub start: u32,
    pub target: u32,
    pub parent: u32,
    /// 规范边索引；起点自身的记录为 [`u32::MAX`]。
    pub edge: u32,
}

/// 全部状态的静默闭包与可回放映射。
#[derive(Debug)]
pub struct ClosureInfo {
    /// `closures[s]`：s 经零或多条 τ 可达的全部状态，升序。
    pub closures: Vec<Vec<u32>>,
    /// 每个 start 一份发现记录（含 start 自身），整体条目数 = 闭包二元组数。
    pub parents: Vec<Vec<ParentEntry>>,
    pub pair_count: usize,
}

const NONE: u32 = u32::MAX;

impl ClosureInfo {
    /// 取 s 的闭包。
    #[must_use]
    pub fn closure(&self, s: u32) -> &[u32] {
        &self.closures[s as usize]
    }

    /// 在回放映射中查 `start ==τ⇒ target` 的一条边序列（升序邻接决定的规范路径）。
    /// 返回 `(状态, 入边)` 序列，首项入边为 [`u32::MAX`]。查不到返回 None（属于内部不一致）。
    #[must_use]
    pub fn replay_path(&self, start: u32, target: u32) -> Option<Vec<(u32, u32)>> {
        let entries = &self.parents[start as usize];
        let mut node = target;
        let mut rev: Vec<(u32, u32)> = Vec::new();
        loop {
            let found = entries
                .iter()
                .find(|p| p.target == node)?;
            rev.push((found.target, found.edge));
            if found.target == start {
                break;
            }
            node = found.parent;
        }
        rev.reverse();
        Some(rev)
    }
}

/// 可复用的位集草稿，避免在内层循环反复分配。
struct Scratch {
    words: Vec<u64>,
    touched: Vec<u32>,
}

impl Scratch {
    fn new(n: usize) -> Self {
        Self {
            words: vec![0; n.div_ceil(64)],
            touched: Vec::new(),
        }
    }

    #[inline]
    fn set(&mut self, i: u32) -> bool {
        let w = (i >> 6) as usize;
        let b = 1u64 << (i & 63);
        let already = self.words[w] & b != 0;
        if !already {
            self.words[w] |= b;
            self.touched.push(i);
        }
        !already
    }

    fn clear(&mut self) {
        for &i in &self.touched {
            self.words[(i >> 6) as usize] = 0;
        }
        self.touched.clear();
    }
}

/// 找一条 τ 边 u -> v 的规范边索引（out[u] 中第一条匹配）。
fn tau_edge_index(lts: &Lts, u: u32, v: u32) -> Option<u32> {
    lts.out[u as usize].iter().copied().find(|&e| {
        let edge = &lts.edges[e as usize];
        edge.label.is_tau() && edge.target == v
    })
}

/// 对单个 LTS 计算全部静默闭包并保留回放映射。
///
/// `pair_budget` 为闭包二元组 + τ 弧扫描工作量的总预算。
fn compute_closure(lts: &Lts, pair_budget: usize) -> Result<ClosureInfo, UnknownReason> {
    let n = lts.num_states();
    let mut closures: Vec<Vec<u32>> = Vec::with_capacity(n);
    let mut parents: Vec<Vec<ParentEntry>> = Vec::with_capacity(n);
    let mut mark = Scratch::new(n);
    let mut pair_count = 0usize;
    // 工作量上限：即使可达对不多，也限制扫描的 τ 弧总数（防止巨型扇出拖垮服务）。
    let scan_budget = pair_budget.saturating_mul(8).max(1024);
    let mut scans = 0usize;

    for start in 0..n as u32 {
        mark.clear();
        let mut entries: Vec<ParentEntry> = Vec::new();
        let mut stack: Vec<u32> = Vec::new();
        mark.set(start);
        entries.push(ParentEntry {
            start,
            target: start,
            parent: NONE,
            edge: NONE,
        });
        stack.push(start);
        pair_count += 1;

        while let Some(u) = stack.pop() {
            for &v in &lts.tau_out[u as usize] {
                scans += 1;
                if scans > scan_budget {
                    return Err(UnknownReason::ClosurePairLimit);
                }
                if mark.set(v) {
                    pair_count += 1;
                    if pair_count > pair_budget {
                        return Err(UnknownReason::ClosurePairLimit);
                    }
                    let edge = tau_edge_index(lts, u, v).unwrap_or(NONE);
                    entries.push(ParentEntry {
                        start,
                        target: v,
                        parent: u,
                        edge,
                    });
                    stack.push(v);
                }
            }
        }

        let mut reached: Vec<u32> = mark.touched.clone();
        reached.sort_unstable();
        closures.push(reached);
        parents.push(entries);
    }

    Ok(ClosureInfo {
        closures,
        parents,
        pair_count,
    })
}

/// 每个状态在每个可观察动作下的弱像。
/// `obs_img[s][a] == None` 表示连直接 a 步都不存在；`Some(set)` 为“a 后再做 τ”的闭包结果。
struct WeakImages {
    obs_img: Vec<Vec<Option<Vec<u32>>>>,
    pair_count: usize,
}

/// 每个状态实际携带的可观察出边（用于避免对空动作做 n×A 扫描）。
fn labeled_successors(lts: &Lts) -> Vec<Vec<(u32, &[u32])>> {
    lts.out
        .iter()
        .map(|es| {
            es.iter()
                .filter_map(|&e| match &lts.edges[e as usize].label {
                    EdgeLabel::Observable { action_idx, .. } => {
                        Some((*action_idx, &lts.obs_out[lts.edges[e as usize].source as usize][*action_idx as usize]))
                    }
                    EdgeLabel::Tau { .. } => None,
                })
                // obs_out 已按动作去重；out 中同一动作可能多条边，按 action_idx 去重。
                .fold(Vec::new(), |mut acc, (a, targets)| {
                    if acc.last().is_none_or(|(prev, _): &(u32, &[u32])| *prev != a) {
                        acc.push((a, targets));
                    }
                    acc
                })
        })
        .collect()
}

/// 每个状态实际携带的可观察出边（用于避免对空动作做 n×A 扫描）。
#[allow(clippy::needless_range_loop)]
fn compute_weak_images(
    lts: &Lts,
    closure: &ClosureInfo,
    alphabet: usize,
    pair_budget: usize,
) -> Result<WeakImages, UnknownReason> {
    let n = lts.num_states();
    let labeled = labeled_successors(lts);
    let mut obs_img: Vec<Vec<Option<Vec<u32>>>> =
        (0..n).map(|_| vec![None; alphabet]).collect();

    // 每状态复用的收集器与位集。
    let mut acc: Vec<Vec<u32>> = (0..alphabet).map(|_| Vec::new()).collect();
    let mut touched: Vec<u32> = Vec::new();
    let mut mark = Scratch::new(n);
    let mut pair_count = 0usize;

    // 循环里同时按下标索引 obs_img / closures / labeled 多张表，保留范围循环更直观。
    for s in 0..n {
        touched.clear();
        // 1) 收集闭包内全部直接 a 目标。
        for &t in &closure.closures[s] {
            for (a, targets) in &labeled[t as usize] {
                if acc[*a as usize].is_empty() {
                    touched.push(*a);
                }
                acc[*a as usize].extend_from_slice(targets);
            }
        }
        // 2) 对每个存在直接步的动作：union C(direct) 得弱像。
        for &a in &touched {
            let direct = &mut acc[a as usize];
            direct.sort_unstable();
            direct.dedup();
            mark.clear();
            for &d in direct.iter() {
                for &t in &closure.closures[d as usize] {
                    mark.set(t);
                }
            }
            let mut set: Vec<u32> = mark.touched.clone();
            set.sort_unstable();
            pair_count += set.len();
            if pair_count > pair_budget {
                return Err(UnknownReason::ClosurePairLimit);
            }
            obs_img[s][a as usize] = Some(set);
            direct.clear();
        }
        // touched 之外的收集器本就为空（clear 在上一轮末尾完成）。
    }

    Ok(WeakImages {
        obs_img,
        pair_count,
    })
}

impl WeakImages {
    /// 状态集 `x`（升序）在动作 `a` 下的弱像，写入 `out`（结果升序去重）。
    fn image_into(&self, x: &[u32], a: u32, mark: &mut Scratch, out: &mut Vec<u32>) {
        mark.clear();
        for &s in x {
            if let Some(set) = &self.obs_img[s as usize][a as usize] {
                for &t in set {
                    mark.set(t);
                }
            }
        }
        out.clear();
        out.extend_from_slice(&mark.touched);
        out.sort_unstable();
    }
}

/// BFS 父指针：经由 `action` 从 `parent` 节点到达。
#[derive(Debug, Clone, Copy)]
struct ParentLink {
    parent: u32,
    action: u32,
}

/// 判定结论（证据报告由独立的 evidence 模块另行构建）。
#[derive(Debug, PartialEq, Eq)]
pub enum KernelVerdict {
    /// 实现弱迹包含于规格。
    Included,
    /// 找到反例：`trace` 为可观察动作索引序列；`impl_reach`/`spec_reach` 为末端子集。
    Counterexample {
        trace: Vec<u32>,
        impl_reach: Vec<u32>,
        spec_reach: Vec<u32>,
    },
    /// 状态爆炸等资源耗尽：不给结论。
    Unknown { reason: UnknownReason },
}

#[derive(Debug, Clone, Serialize)]
pub struct SolveStats {
    pub spec_states: usize,
    pub spec_edges: usize,
    pub impl_states: usize,
    pub impl_edges: usize,
    pub alphabet_size: usize,
    pub spec_closure_pairs: usize,
    pub impl_closure_pairs: usize,
    pub spec_image_pairs: usize,
    pub impl_image_pairs: usize,
    pub search_nodes_visited: usize,
    pub search_layers: usize,
    pub max_frontier: usize,
    pub limits: SerializableLimits,
}

#[derive(Debug, Clone, Serialize)]
pub struct SerializableLimits {
    pub max_states_per_lts: usize,
    pub max_edges_per_lts: usize,
    pub max_alphabet: usize,
    pub max_closure_pairs: usize,
    pub max_search_nodes: usize,
}

impl From<&SolverLimits> for SerializableLimits {
    fn from(l: &SolverLimits) -> Self {
        Self {
            max_states_per_lts: l.max_states_per_lts,
            max_edges_per_lts: l.max_edges_per_lts,
            max_alphabet: l.max_alphabet,
            max_closure_pairs: l.max_closure_pairs,
            max_search_nodes: l.max_search_nodes,
        }
    }
}

pub struct KernelOutput {
    pub verdict: KernelVerdict,
    pub stats: SolveStats,
}

/// 运行一次完整检查。闭包与弱像先算好并保留回放映射，再做子集对搜索。
pub fn solve(
    system: &AlignedSystem,
    limits: &SolverLimits,
) -> Result<KernelOutput, KernelError> {
    let spec = &system.specification;
    let impls = &system.implementation;
    let alphabet = system.alphabet.len();

    let stats_for_unknown = |_reason: UnknownReason| SolveStats {
        spec_states: spec.num_states(),
        spec_edges: spec.edges.len(),
        impl_states: impls.num_states(),
        impl_edges: impls.edges.len(),
        alphabet_size: alphabet,
        spec_closure_pairs: 0,
        impl_closure_pairs: 0,
        spec_image_pairs: 0,
        impl_image_pairs: 0,
        search_nodes_visited: 0,
        search_layers: 0,
        max_frontier: 0,
        limits: SerializableLimits::from(limits),
    };

    let spec_closure = match compute_closure(spec, limits.max_closure_pairs) {
        Ok(c) => c,
        Err(reason) => {
            return Ok(KernelOutput {
                verdict: KernelVerdict::Unknown { reason },
                stats: stats_for_unknown(reason),
            })
        }
    };
    let impl_closure = match compute_closure(impls, limits.max_closure_pairs) {
        Ok(c) => c,
        Err(reason) => {
            return Ok(KernelOutput {
                verdict: KernelVerdict::Unknown { reason },
                stats: stats_for_unknown(reason),
            })
        }
    };
    let spec_images =
        match compute_weak_images(spec, &spec_closure, alphabet, limits.max_closure_pairs) {
            Ok(w) => w,
            Err(reason) => {
                return Ok(KernelOutput {
                    verdict: KernelVerdict::Unknown { reason },
                    stats: stats_for_unknown(reason),
                })
            }
        };
    let impl_images =
        match compute_weak_images(impls, &impl_closure, alphabet, limits.max_closure_pairs) {
            Ok(w) => w,
            Err(reason) => {
                return Ok(KernelOutput {
                    verdict: KernelVerdict::Unknown { reason },
                    stats: stats_for_unknown(reason),
                })
            }
        };

    let stats_base = SolveStats {
        spec_states: spec.num_states(),
        spec_edges: spec.edges.len(),
        impl_states: impls.num_states(),
        impl_edges: impls.edges.len(),
        alphabet_size: alphabet,
        spec_closure_pairs: spec_closure.pair_count,
        impl_closure_pairs: impl_closure.pair_count,
        spec_image_pairs: spec_images.pair_count,
        impl_image_pairs: impl_images.pair_count,
        search_nodes_visited: 0,
        search_layers: 0,
        max_frontier: 0,
        limits: SerializableLimits::from(limits),
    };

    let result = subset_pair_bfs(
        system,
        &spec_closure,
        &impl_closure,
        &spec_images,
        &impl_images,
        limits,
    );

    match result {
        Ok(BfsResult {
            verdict,
            nodes_visited,
            layers,
            max_frontier,
        }) => {
            let mut stats = stats_base;
            stats.search_nodes_visited = nodes_visited;
            stats.search_layers = layers;
            stats.max_frontier = max_frontier;
            Ok(KernelOutput { verdict, stats })
        }
        Err(exhausted) => {
            let mut stats = stats_base;
            stats.search_nodes_visited = exhausted.nodes_visited;
            stats.search_layers = exhausted.layers;
            stats.max_frontier = exhausted.max_frontier;
            Ok(KernelOutput {
                verdict: KernelVerdict::Unknown {
                    reason: exhausted.reason,
                },
                stats,
            })
        }
    }
}

struct BfsResult {
    verdict: KernelVerdict,
    nodes_visited: usize,
    layers: usize,
    max_frontier: usize,
}

/// 资源耗尽时也带回已经完成的搜索量，供诊断展示。
struct Exhausted {
    reason: UnknownReason,
    nodes_visited: usize,
    layers: usize,
    max_frontier: usize,
}

/// 子集对分层 BFS。
#[allow(clippy::too_many_arguments)]
fn subset_pair_bfs(
    system: &AlignedSystem,
    spec_closure: &ClosureInfo,
    impl_closure: &ClosureInfo,
    spec_images: &WeakImages,
    impl_images: &WeakImages,
    limits: &SolverLimits,
) -> Result<BfsResult, Exhausted> {
    let alphabet = system.alphabet.len() as u32;
    let n_spec = system.specification.num_states();
    let n_impl = system.implementation.num_states();

    // 初始闭包集合（升序）。
    let mut init_i: Vec<u32> = Vec::new();
    {
        let mut mark = Scratch::new(n_impl);
        for &s in &system.implementation.initial_states {
            for &t in impl_closure.closure(s) {
                mark.set(t);
            }
        }
        init_i.extend(mark.touched.iter().copied());
        init_i.sort_unstable();
    }
    let mut init_s: Vec<u32> = Vec::new();
    {
        let mut mark = Scratch::new(n_spec);
        for &s in &system.specification.initial_states {
            for &t in spec_closure.closure(s) {
                mark.set(t);
            }
        }
        init_s.extend(mark.touched.iter().copied());
        init_s.sort_unstable();
    }

    // 初始节点不可能是反例（解析阶段保证两侧初态非空，闭包至少含自身）。
    let mut parents: Vec<Option<ParentLink>> = vec![None];
    let mut i_sets: Vec<Vec<u32>> = vec![init_i.clone()];
    let mut s_sets: Vec<Vec<u32>> = vec![init_s.clone()];
    let mut visited: hashbrownish::Set = hashbrownish::new();
    visited.insert(hashbrownish::Key {
        i: init_i,
        s: init_s,
    });

    let mut layer: Vec<u32> = vec![0];
    let mut nodes_visited = 1usize;
    let mut layers = 0usize;
    let mut max_frontier = 1usize;

    let mut mark_i = Scratch::new(n_impl);
    let mut mark_s = Scratch::new(n_spec);
    let mut next_i = Vec::new();
    let mut next_s = Vec::new();

    loop {
        layers += 1;
        let mut next_layer: Vec<u32> = Vec::new();
        // 节点按上一层生成顺序（= 迹字典序）处理；动作按字母表升序尝试。
        for &node in &layer {
            for a in 0..alphabet {
                impl_images.image_into(&i_sets[node as usize], a, &mut mark_i, &mut next_i);
                spec_images.image_into(&s_sets[node as usize], a, &mut mark_s, &mut next_s);

                if !next_i.is_empty() && next_s.is_empty() {
                    // 反例：沿父指针重建动作序列。
                    let mut trace = vec![a];
                    let mut cur = node;
                    while let Some(link) = parents[cur as usize] {
                        trace.push(link.action);
                        cur = link.parent;
                    }
                    trace.reverse();
                    return Ok(BfsResult {
                        verdict: KernelVerdict::Counterexample {
                            trace,
                            impl_reach: next_i.clone(),
                            spec_reach: Vec::new(),
                        },
                        nodes_visited,
                        layers,
                        max_frontier,
                    });
                }

                // 规格侧已空但实现侧也空：此动作谁都做不了，不是反例，也无需扩展。
                if next_i.is_empty() {
                    continue;
                }

                let key = hashbrownish::Key {
                    i: next_i.clone(),
                    s: next_s.clone(),
                };
                if !visited.contains(&key) {
                    if nodes_visited >= limits.max_search_nodes {
                        return Err(Exhausted {
                            reason: UnknownReason::SearchNodeLimit,
                            nodes_visited,
                            layers,
                            max_frontier,
                        });
                    }
                    let id = parents.len() as u32;
                    parents.push(Some(ParentLink {
                        parent: node,
                        action: a,
                    }));
                    i_sets.push(key.i.clone());
                    s_sets.push(key.s.clone());
                    visited.insert(key);
                    next_layer.push(id);
                    nodes_visited += 1;
                }
            }
        }

        if next_layer.is_empty() {
            return Ok(BfsResult {
                verdict: KernelVerdict::Included,
                nodes_visited,
                layers,
                max_frontier,
            });
        }
        max_frontier = max_frontier.max(next_layer.len());
        layer = next_layer;
    }
}

/// 极小的 HashSet 包装：键是两个升序状态集。之所以不用“单步动作签名”之类的商集键，
/// 是因为弱迹包含必须区分到达方式不同的具体状态集合（需求明确禁止按单步动作相同当等价）。
mod hashbrownish {
    use std::collections::HashSet;
    use std::hash::{Hash, Hasher};

    #[derive(Clone)]
    pub struct Key {
        pub i: Vec<u32>,
        pub s: Vec<u32>,
    }

    impl PartialEq for Key {
        fn eq(&self, other: &Self) -> bool {
            self.i == other.i && self.s == other.s
        }
    }
    impl Eq for Key {}
    impl Hash for Key {
        fn hash<H: Hasher>(&self, state: &mut H) {
            self.i.hash(state);
            self.s.hash(state);
        }
    }

    pub type Set = HashSet<Key>;

    pub fn new() -> Set {
        HashSet::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::input;
    use crate::model::{CheckRequestWire, EdgeWire, LtsWire};

    fn tiny_system(spec_edges: Vec<EdgeWire>, impl_edges: Vec<EdgeWire>) -> AlignedSystem {
        let req = CheckRequestWire {
            run_id: None,
            observable_actions: vec!["a".to_owned()],
            specification: LtsWire {
                name: "s".into(),
                states: vec!["s0".into(), "s1".into(), "s2".into()],
                initial_states: vec!["s0".into()],
                edges: spec_edges,
                hidden_actions: vec!["t".into()],
            },
            implementation: LtsWire {
                name: "i".into(),
                states: vec!["i0".into()],
                initial_states: vec!["i0".into()],
                edges: impl_edges,
                hidden_actions: vec![],
            },
            limits: None,
        };
        input::parse_request(req).unwrap().0
    }

    #[test]
    fn closure_replay_path_walks_only_tau_edges() {
        // s0 --t--> s1 --t--> s2，另有 s0 --a--> s2（观察边不得被闭包使用）。
        let system = tiny_system(
            vec![
                EdgeWire { id: None, source: "s0".into(), action: "t".into(), target: "s1".into() },
                EdgeWire { id: None, source: "s1".into(), action: "t".into(), target: "s2".into() },
                EdgeWire { id: None, source: "s0".into(), action: "a".into(), target: "s2".into() },
            ],
            vec![EdgeWire { id: None, source: "i0".into(), action: "a".into(), target: "i0".into() }],
        );
        let c = compute_closure(&system.specification, 10_000).unwrap();
        assert_eq!(c.closure(0), &[0, 1, 2]);
        assert_eq!(c.closure(1), &[1, 2]);

        // 回放 s0 ⇒ s2：路径应为 s0 -e-> s1 -e-> s2，且每条边确实是 τ。
        let path = c.replay_path(0, 2).expect("应存在回放路径");
        assert_eq!(path[0], (0, u32::MAX));
        assert_eq!(path.len(), 3);
        for (state, edge) in path.iter().skip(1) {
            let e = &system.specification.edges[*edge as usize];
            assert!(e.label.is_tau());
            assert_eq!(e.target, *state);
        }
        assert_eq!(path.last().unwrap().0, 2);

        // 不可达对返回 None。
        assert!(compute_closure(&system.specification, 10_000)
            .unwrap()
            .replay_path(2, 0)
            .is_none());
    }

    #[test]
    fn tau_loop_closure_is_fixpoint() {
        let system = tiny_system(
            vec![
                EdgeWire { id: None, source: "s0".into(), action: "t".into(), target: "s1".into() },
                EdgeWire { id: None, source: "s1".into(), action: "t".into(), target: "s0".into() },
                EdgeWire { id: None, source: "s0".into(), action: "a".into(), target: "s0".into() },
            ],
            vec![EdgeWire { id: None, source: "i0".into(), action: "a".into(), target: "i0".into() }],
        );
        let c = compute_closure(&system.specification, 10_000).unwrap();
        assert_eq!(c.closure(0), &[0, 1]);
        assert_eq!(c.closure(1), &[0, 1]);
    }
}
