//! 证据验证：独立复核“一组约束 ID 是否构成严格负环”。
//!
//! 该模块刻意不调用求解内核：它直接从约束文本 `x - y <= c` 重算图边，
//! 检查环是否闭合、费用是否严格为负（checked 加法），
//! 从而保证冲突证据不是内核“自说自话”。
//!
//! 结构/语义不成立（不闭合、费用非负）返回 `Ok(Err(reason))`（HTTP 200 + valid=false）；
//! 输入、状态、计算类问题返回 [`VerifyError`]，由接口层映射为 4xx。

use std::collections::BTreeMap;

use crate::model::{ConflictDto, ConstraintInput, EvidenceEdgeDto};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum VerifyError {
    /// 请求本身不合法（如空环、环名重复）。
    Input(String),
    /// 引用了当前集合中不存在的约束 ID。
    UnknownConstraint(String),
    /// 环费用求和发生整数溢出。
    ArithmeticOverflow(String),
}

/// 复核一个按行走顺序给出的约束名环。
///
/// - `Ok(Ok(evidence))`：环闭合且费用严格为负；
/// - `Ok(Err(reason))`：给出的序列不构成冲突环（200/valid=false 的原因）；
/// - `Err(..)`：输入/状态/计算类问题。
pub fn verify_cycle(
    cycle_names: &[String],
    by_name: &BTreeMap<String, ConstraintInput>,
) -> Result<Result<ConflictDto, String>, VerifyError> {
    if cycle_names.is_empty() {
        return Err(VerifyError::Input(
            "cycle must contain at least one constraint id".to_string(),
        ));
    }

    let mut seen = std::collections::HashSet::new();
    for name in cycle_names {
        if !seen.insert(name.as_str()) {
            return Err(VerifyError::Input(format!(
                "constraint id repeats within submitted cycle: {name}"
            )));
        }
    }

    // 直接按输入语言重算边：x - y <= c  =>  edge y -> x, weight c。
    let mut edges: Vec<EvidenceEdgeDto> = Vec::with_capacity(cycle_names.len());
    for name in cycle_names {
        let c = by_name
            .get(name)
            .ok_or_else(|| VerifyError::UnknownConstraint(name.clone()))?;
        edges.push(EvidenceEdgeDto {
            constraint: c.name.clone(),
            from: c.y.clone(),
            to: c.x.clone(),
            weight: c.c,
        });
    }

    let len = edges.len();
    for i in 0..len {
        let cur_to = &edges[i].to;
        let next_from = &edges[(i + 1) % len].from;
        if cur_to != next_from {
            return Ok(Err(format!(
                "cycle not closed: edge {} ends at variable {cur_to:?} but next edge {} starts at {next_from:?}",
                edges[i].constraint, edges[(i + 1) % len].constraint
            )));
        }
    }

    let mut total = 0_i64;
    for e in &edges {
        total = total.checked_add(e.weight).ok_or_else(|| {
            VerifyError::ArithmeticOverflow(format!(
                "summing cycle cost at constraint {} (weight {})",
                e.constraint, e.weight
            ))
        })?;
    }

    if total >= 0 {
        return Ok(Err(format!(
            "cycle is closed but its cost is {total}, which is not strictly negative"
        )));
    }

    Ok(Ok(ConflictDto {
        cycle: edges,
        total_cost: total,
    }))
}
