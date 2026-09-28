//! 带容量的普通带权 Petri 网数据模型。
//!
//! 约定：
//! - 库所（place）显式携带容量上界 `capacity`，本系统只处理带显式容量的网；
//! - 弧权为非负整数，0 表示该弧不参与消耗/生成（输入时直接拒绝 0 权弧，模型层允许以简化构造）；
//! - 标识（marking）是每个库所上的令牌数，任何合法标识都满足 `0 <= tokens <= capacity`；
//! - 本模型不做类型化令牌（colored net），即“普通”网：令牌是无差别的。

use std::collections::HashMap;

/// 库所索引（网内按出现顺序编号）。
pub type PlaceId = usize;
/// 变迁索引（网内按出现顺序编号）。
pub type TransitionId = usize;

/// 单条有向弧（库所 <-> 变迁）及其整数权重。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArcExpr {
    /// 弧另一端的库所索引。
    pub place: PlaceId,
    /// 弧权（正整数）。
    pub weight: i64,
}

/// 库所：名称与显式容量上界。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Place {
    pub name: String,
    pub capacity: i64,
}

/// 变迁：名称以及全部输入弧、输出弧。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Transition {
    pub name: String,
    /// 输入弧（消耗），按库所索引有序保存。
    pub inputs: Vec<ArcExpr>,
    /// 输出弧（生成），按库所索引有序保存。
    pub outputs: Vec<ArcExpr>,
}

/// 普通带权 Petri 网（所有库所带显式容量上界）。
#[derive(Debug, Clone)]
pub struct Net {
    pub places: Vec<Place>,
    pub transitions: Vec<Transition>,
}

/// 标识：每个库所上的令牌数向量。
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Marking(pub Vec<i64>);

impl Net {
    pub fn place_count(&self) -> usize {
        self.places.len()
    }

    pub fn transition_count(&self) -> usize {
        self.transitions.len()
    }

    /// 按库所名查找索引（O(n)，网通常较小；API 层会先做名校验）。
    pub fn place_index(&self, name: &str) -> Option<PlaceId> {
        self.places.iter().position(|p| p.name == name)
    }

    /// 按变迁名查找索引。
    pub fn transition_index(&self, name: &str) -> Option<TransitionId> {
        self.transitions.iter().position(|t| t.name == name)
    }

    /// 网的库所名 -> 索引映射。
    pub fn place_map(&self) -> HashMap<&str, PlaceId> {
        self.places
            .iter()
            .enumerate()
            .map(|(i, p)| (p.name.as_str(), i))
            .collect()
    }
}

impl Marking {
    pub fn new(tokens: Vec<i64>) -> Self {
        Marking(tokens)
    }

    pub fn len(&self) -> usize {
        self.0.len()
    }

    pub fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    /// 令牌总数（用于令牌守恒/加权守恒断言）。
    pub fn total_tokens(&self) -> i128 {
        self.0.iter().map(|&x| i128::from(x)).sum()
    }
}
