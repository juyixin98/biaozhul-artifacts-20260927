//! 输入语言的线缆类型（请求/响应 JSON）与求解内核使用的内部表示。
//!
//! 输入语言示例见 `demo/` 下的夹具：一个 LTS 由具名状态、可选的显式状态表、
//! 初始状态集合和边组成；动作通过全局 `observable_actions` 声明为可观察，
//! 其余动作只有在某一侧 `hidden_actions` 中声明时才允许出现（静默动作 τ）。

use serde::{Deserialize, Serialize};

// ============================== 线缆类型 ==============================

/// 单条迁移边。`id` 可由调用方给出，用于在证据中回放；为空时由系统分配规范索引。
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct EdgeWire {
    #[serde(default)]
    pub id: Option<String>,
    pub source: String,
    pub action: String,
    pub target: String,
}

/// 线缆层的 LTS：名字仍是字符串，尚未做任何索引化。
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct LtsWire {
    pub name: String,
    /// 显式声明的状态表；出现在边/初态里但未声明的状态会补登，冲突另行报错。
    #[serde(default)]
    pub states: Vec<String>,
    pub initial_states: Vec<String>,
    #[serde(default)]
    pub edges: Vec<EdgeWire>,
    /// 仅在本侧视为静默（τ）的动作名。
    #[serde(default)]
    pub hidden_actions: Vec<String>,
}

/// 一次弱迹包含检查请求：`implementation` 是否弱迹包含于 `specification`。
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct CheckRequestWire {
    #[serde(default)]
    pub run_id: Option<String>,
    pub observable_actions: Vec<String>,
    pub specification: LtsWire,
    pub implementation: LtsWire,
    /// 可选的资源限制覆盖；缺省值与硬上限见 [`crate::solver::SolverLimits`]。
    #[serde(default)]
    pub limits: Option<LimitsWire>,
}

/// 调用方可覆盖的资源限制。所有字段可选，未给则使用默认值。
#[derive(Debug, Clone, Default, Deserialize, Serialize)]
pub struct LimitsWire {
    #[serde(default)]
    pub max_states_per_lts: Option<usize>,
    #[serde(default)]
    pub max_edges_per_lts: Option<usize>,
    #[serde(default)]
    pub max_alphabet: Option<usize>,
    /// 静默闭包最多保留的 (状态 -> 可达状态) 二元组数量。
    #[serde(default)]
    pub max_closure_pairs: Option<usize>,
    /// 子集对搜索最多访问的 BFS 节点数；超出即返回 unknown。
    #[serde(default)]
    pub max_search_nodes: Option<usize>,
}

// ============================== 内部表示 ==============================

/// 一条边的动作：要么是可观察动作（携带对齐后的字母表索引），要么是静默 τ。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EdgeLabel {
    /// 可观察动作，`action_idx` 是全局对齐字母表中的索引。
    Observable { action_idx: u32, name: String },
    /// 静默动作；`name` 只用于诊断展示，不参与可观察迹。
    Tau { name: String },
}

impl EdgeLabel {
    #[must_use]
    pub fn is_tau(&self) -> bool {
        matches!(self, EdgeLabel::Tau { .. })
    }

    #[must_use]
    pub fn display_name(&self) -> &str {
        match self {
            EdgeLabel::Observable { name, .. } | EdgeLabel::Tau { name } => name,
        }
    }
}

/// 内部索引化后的迁移边。`canon_index` 是确定性排序后的规范下标（即证据中的 edge 0,1,...）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Edge {
    pub canon_index: u32,
    /// 调用方提供的边 id（若有）。
    pub user_id: Option<String>,
    pub source: u32,
    pub target: u32,
    pub label: EdgeLabel,
}

/// 求解内核使用的有限标号迁移系统：所有名字已索引化。
#[derive(Debug, Clone)]
pub struct Lts {
    pub name: String,
    pub state_names: Vec<String>,
    pub initial_states: Vec<u32>,
    /// 按规范下标排序的全部边。
    pub edges: Vec<Edge>,
    /// `out[s]` 为源点为 s 的边的规范下标列表，按 (动作序, 目标序, 边id) 排序。
    pub out: Vec<Vec<u32>>,
    /// 对每个可观察动作 a：`obs_out[s][a]` 为从 s 经 a 直接出发的目标状态（已去重排序）。
    pub obs_out: Vec<Vec<Vec<u32>>>,
    /// `tau_out[s]` 为从 s 经 τ 边直达的目标（已去重排序）。
    pub tau_out: Vec<Vec<u32>>,
    /// 本侧声明的静默动作名（诊断用）。
    pub hidden_names: Vec<String>,
}

impl Lts {
    #[must_use]
    pub fn num_states(&self) -> usize {
        self.state_names.len()
    }

    #[must_use]
    pub fn state_name(&self, idx: u32) -> &str {
        &self.state_names[idx as usize]
    }

    #[must_use]
    pub fn edge(&self, canon_index: u32) -> &Edge {
        &self.edges[canon_index as usize]
    }
}

/// 两侧 LTS 连同对齐后的可观察字母表。
#[derive(Debug, Clone)]
pub struct AlignedSystem {
    pub alphabet: Vec<String>,
    pub specification: Lts,
    pub implementation: Lts,
}

impl AlignedSystem {
    /// 取指定一侧（"specification" / "implementation"），仅供报告使用。
    #[must_use]
    pub fn side(&self, which: Side) -> &Lts {
        match which {
            Side::Specification => &self.specification,
            Side::Implementation => &self.implementation,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Specification,
    Implementation,
}

impl Side {
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Side::Specification => "specification",
            Side::Implementation => "implementation",
        }
    }
}
