//! 证据验证：独立重放发射序列、独立核对 P 不变量候选。
//!
//! 本模块刻意**不调用**内核的 `fire`/`is_enabled`，而是从网结构出发重新执行
//! 每条弧的消耗与生成，使证据不是“被测核心实现自己证明自己”。

use serde::Serialize;

use crate::kernel::model::Net;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StepDeficit {
    pub place: String,
    pub have: i64,
    pub need: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct StepOverflow {
    pub place: String,
    pub capacity: i64,
    pub resulting: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InvalidStep {
    pub index: usize,
    pub transition: String,
    pub deficits: Vec<StepDeficit>,
    pub overflows: Vec<StepOverflow>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ReplayError {
    UnknownTransition {
        name: String,
        index: usize,
    },
    TransitionNotEnabled {
        step: InvalidStep,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct ReplayReport {
    pub valid: bool,
    /// M0, M1, …, Mk：逐步标识轨迹（独立重放得到）。
    pub reached: Vec<Vec<i64>>,
    pub final_marking: Vec<i64>,
    pub steps_fired: usize,
    /// 每一步是否合法（独立重放结论）。
    pub step_valid: Vec<bool>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum InvariantError {
    WeightCount { expected: usize, got: usize },
    ZeroWeights,
    NegativeWeight { place: String, weight: i64 },
    WeightTooLarge { place: String, weight: i64 },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InvariantCheckReport {
    pub valid: bool,
    pub weights: Vec<i64>,
    /// 按变迁顺序的守恒残差 C·y 分量，全 0 才是守恒律。
    pub residual: Vec<i128>,
    pub residual_max_abs: i128,
    pub is_nonnegative: bool,
    pub support: Vec<String>,
    pub initial_weighted_sum: i128,
    pub target_weighted_sum: Option<i128>,
    pub weighted_sum_matches: Option<bool>,
}

fn index_by_name(net: &Net, name: &str) -> Option<usize> {
    net.transitions.iter().position(|t| t.name == name)
}

/// 独立重放：逐个变迁检查全部输入弧，原子检查容量，再执行消耗/生成。
pub fn replay(
    net: &Net,
    initial: &[i64],
    sequence: &[String],
) -> Result<ReplayReport, ReplayError> {
    let mut m: Vec<i64> = initial.to_vec();
    let mut reached: Vec<Vec<i64>> = vec![initial.to_vec()];
    let mut step_valid = Vec::new();

    for (idx, name) in sequence.iter().enumerate() {
        let Some(ti) = index_by_name(net, name) else {
            return Err(ReplayError::UnknownTransition {
                name: name.clone(),
                index: idx,
            });
        };
        let t = &net.transitions[ti];

        // 全部输入弧同时检查。
        let mut deficits = Vec::new();
        for arc in &t.inputs {
            let have = m[arc.place];
            if have < arc.weight {
                deficits.push(StepDeficit {
                    place: net.places[arc.place].name.clone(),
                    have,
                    need: arc.weight,
                });
            }
        }
        // 净增量与容量原子检查（用 i128 防中间溢出）。
        let mut overflows = Vec::new();
        let mut delta = vec![0i128; net.place_count()];
        for arc in &t.inputs {
            delta[arc.place] -= i128::from(arc.weight);
        }
        for arc in &t.outputs {
            delta[arc.place] += i128::from(arc.weight);
        }
        for (p, d) in delta.iter().enumerate() {
            let r = i128::from(m[p]) + d;
            if r > i128::from(net.places[p].capacity) {
                overflows.push(StepOverflow {
                    place: net.places[p].name.clone(),
                    capacity: net.places[p].capacity,
                    resulting: i64::try_from(r).unwrap_or(i64::MAX),
                });
            }
        }

        if !deficits.is_empty() || !overflows.is_empty() {
            return Err(ReplayError::TransitionNotEnabled {
                step: InvalidStep {
                    index: idx,
                    transition: name.clone(),
                    deficits,
                    overflows,
                },
            });
        }

        // 合法：原子消耗与生成。
        for arc in &t.inputs {
            m[arc.place] -= arc.weight;
        }
        for arc in &t.outputs {
            m[arc.place] += arc.weight;
        }
        step_valid.push(true);
        reached.push(m.clone());
    }

    Ok(ReplayReport {
        valid: true,
        reached,
        final_marking: m,
        steps_fired: sequence.len(),
        step_valid,
    })
}

/// 独立核对 P 不变量候选（权向量形状、非负性、守恒残差，以及可选的初/目标加权和）。
pub fn check_invariant(
    net: &Net,
    weights: &[i64],
    initial: Option<&[i64]>,
    target: Option<&[i64]>,
) -> Result<InvariantCheckReport, InvariantError> {
    if weights.len() != net.place_count() {
        return Err(InvariantError::WeightCount {
            expected: net.place_count(),
            got: weights.len(),
        });
    }
    if weights.iter().all(|&w| w == 0) {
        return Err(InvariantError::ZeroWeights);
    }
    for (i, &w) in weights.iter().enumerate() {
        if w < 0 {
            return Err(InvariantError::NegativeWeight {
                place: net.places[i].name.clone(),
                weight: w,
            });
        }
        if w > i64::MAX / 4 {
            return Err(InvariantError::WeightTooLarge {
                place: net.places[i].name.clone(),
                weight: w,
            });
        }
    }

    let mut residual = Vec::with_capacity(net.transition_count());
    for t in &net.transitions {
        let mut r: i128 = 0;
        for arc in &t.inputs {
            r += i128::from(weights[arc.place]) * i128::from(arc.weight);
        }
        for arc in &t.outputs {
            r -= i128::from(weights[arc.place]) * i128::from(arc.weight);
        }
        residual.push(r);
    }
    let residual_max_abs = residual
        .iter()
        .map(|r| r.unsigned_abs() as i128)
        .max()
        .unwrap_or(0);

    let support: Vec<String> = net
        .places
        .iter()
        .zip(weights.iter())
        .filter(|(_, &w)| w > 0)
        .map(|(p, _)| p.name.clone())
        .collect();

    let wsum = |m: &[i64]| -> i128 {
        weights
            .iter()
            .zip(m.iter())
            .map(|(&w, &tok)| i128::from(w) * i128::from(tok))
            .sum()
    };

    let s0 = initial.map(wsum);
    let st = target.map(wsum);
    let matches = match (s0, st) {
        (Some(a), Some(b)) => Some(a == b),
        _ => None,
    };

    Ok(InvariantCheckReport {
        valid: residual_max_abs == 0,
        weights: weights.to_vec(),
        residual,
        residual_max_abs,
        is_nonnegative: true,
        support,
        initial_weighted_sum: s0.unwrap_or(0),
        target_weighted_sum: st,
        weighted_sum_matches: matches,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kernel::model::{ArcExpr, Net, Place, Transition};

    fn two_place_net() -> Net {
        Net {
            places: vec![
                Place { name: "p0".into(), capacity: 3 },
                Place { name: "p1".into(), capacity: 3 },
            ],
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![ArcExpr { place: 0, weight: 1 }],
                outputs: vec![ArcExpr { place: 1, weight: 1 }],
            }],
        }
    }

    #[test]
    fn replay_independently_confirms_valid_sequence() {
        let net = two_place_net();
        let r = replay(&net, &[1, 0], &["t".to_string()]).unwrap();
        assert!(r.valid);
        assert_eq!(r.final_marking, vec![0, 1]);
        assert_eq!(r.reached, vec![vec![1, 0], vec![0, 1]]);
        assert_eq!(r.step_valid, vec![true]);
    }

    #[test]
    fn replay_flags_input_deficit_and_overflow_categories() {
        let net = two_place_net();
        let err = replay(&net, &[0, 0], &["t".to_string()]).unwrap_err();
        match err {
            ReplayError::TransitionNotEnabled { step } => {
                assert_eq!(step.index, 0);
                assert_eq!(step.deficits.len(), 1);
                assert_eq!(step.deficits[0].need, 1);
                assert!(step.overflows.is_empty());
            }
            other => panic!("expected not-enabled, got {other:?}"),
        }

        // 容量溢出：单库所容量 1，自发生成 2。
        let net2 = Net {
            places: vec![Place { name: "p".into(), capacity: 1 }],
            transitions: vec![Transition {
                name: "g".into(),
                inputs: vec![],
                outputs: vec![ArcExpr { place: 0, weight: 2 }],
            }],
        };
        let err2 = replay(&net2, &[0], &["g".to_string()]).unwrap_err();
        match err2 {
            ReplayError::TransitionNotEnabled { step } => {
                assert_eq!(step.overflows.len(), 1);
                assert_eq!(step.overflows[0].resulting, 2);
                assert!(step.deficits.is_empty());
            }
            other => panic!("expected overflow, got {other:?}"),
        }
    }

    #[test]
    fn replay_unknown_transition_is_distinct_error() {
        let net = two_place_net();
        let err = replay(&net, &[1, 0], &["nope".into()]).unwrap_err();
        assert!(matches!(err, ReplayError::UnknownTransition { index: 0, .. }));
    }

    #[test]
    fn invariant_check_validates_residual_and_weighted_sums() {
        let net = two_place_net();
        let ok = check_invariant(&net, &[1, 1], Some(&[1, 0]), Some(&[0, 1])).unwrap();
        assert!(ok.valid);
        assert_eq!(ok.residual, vec![0]);
        assert_eq!(ok.weighted_sum_matches, Some(true));

        // 加权和不同：守恒律仍成立，但初/目标加权和不等。
        let bad = check_invariant(&net, &[1, 1], Some(&[1, 0]), Some(&[1, 1])).unwrap();
        assert!(bad.valid);
        assert_eq!(bad.weighted_sum_matches, Some(false));

        // 非守恒向量。
        let not_inv = check_invariant(&net, &[1, 0], None, None).unwrap();
        assert!(!not_inv.valid);
        assert_ne!(not_inv.residual_max_abs, 0);
    }

    #[test]
    fn invariant_check_rejects_bad_shapes() {
        let net = two_place_net();
        assert!(matches!(
            check_invariant(&net, &[1], None, None),
            Err(InvariantError::WeightCount { expected: 2, got: 1 })
        ));
        assert!(matches!(
            check_invariant(&net, &[0, 0], None, None),
            Err(InvariantError::ZeroWeights)
        ));
        assert!(matches!(
            check_invariant(&net, &[-1, 1], None, None),
            Err(InvariantError::NegativeWeight { .. })
        ));
    }
}
