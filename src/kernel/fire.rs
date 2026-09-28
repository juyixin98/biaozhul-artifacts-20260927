//! 变迁启用与发射语义（行为契约 1、2）。
//!
//! - 启用判定同时检查**全部**输入弧的令牌需求；任一库所不足即禁用，并记录全部缺口。
//! - 容量是发射前提的一部分：先计算完整增量，原子地检查消耗后不超容量，禁止截断。
//! - 发射是原子消耗与生成：只有所有前提同时成立时才产出新标识，否则返回完整阻断原因。

use serde::Serialize;

use super::model::{Marking, Net, TransitionId};

/// 某个输入库所令牌不足。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct InputDeficit {
    pub place: String,
    pub place_index: usize,
    pub have: i64,
    pub need: i64,
}

/// 发射后某个库所会超出显式容量上界。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PlaceOverflow {
    pub place: String,
    pub place_index: usize,
    pub capacity: i64,
    pub resulting: i64,
}

/// 变迁在某标识下被阻断的全部原因（输入不足与容量溢出可能同时出现，一并报告）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FireBlocked {
    pub insufficient_inputs: Vec<InputDeficit>,
    pub capacity_overflows: Vec<PlaceOverflow>,
}

impl FireBlocked {
    pub fn is_blocked(&self) -> bool {
        !self.insufficient_inputs.is_empty() || !self.capacity_overflows.is_empty()
    }
}

/// 内核调用层面的错误（非法参数），与“合法但被阻断”相区别。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KernelError {
    UnknownTransition(String),
    MarkingLength { expected: usize, got: usize },
    ArithmeticOverflow,
}

impl std::fmt::Display for KernelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            KernelError::UnknownTransition(t) => write!(f, "unknown transition: {t}"),
            KernelError::MarkingLength { expected, got } => {
                write!(f, "marking length {got} does not match place count {expected}")
            }
            KernelError::ArithmeticOverflow => write!(f, "integer arithmetic overflow"),
        }
    }
}

impl std::error::Error for KernelError {}

/// 发射失败：要么变迁合法但被前提阻断，要么调用参数非法。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FireError {
    Blocked(FireBlocked),
    Invalid(KernelError),
}

/// 返回变迁未被启用的全部原因；启用时返回 `None`。
pub fn why_not_enabled(
    net: &Net,
    marking: &Marking,
    transition: TransitionId,
) -> Result<Option<FireBlocked>, KernelError> {
    if marking.len() != net.place_count() {
        return Err(KernelError::MarkingLength {
            expected: net.place_count(),
            got: marking.len(),
        });
    }
    let t = net
        .transitions
        .get(transition)
        .ok_or_else(|| KernelError::UnknownTransition(format!("index:{transition}")))?;

    // 1) 全部输入弧同时满足。
    let mut deficits = Vec::new();
    for arc in &t.inputs {
        let have = marking.0[arc.place];
        if have < arc.weight {
            deficits.push(InputDeficit {
                place: net.places[arc.place].name.clone(),
                place_index: arc.place,
                have,
                need: arc.weight,
            });
        }
    }

    // 2) 原子计算每个库所的净增量并检查容量边界（自环：净增量可能为负，天然处理）。
    let mut overflows = Vec::new();
    let net_delta = net_change(net, t)?;
    for (p, delta) in net_delta.iter().enumerate() {
        let resulting = i128::from(marking.0[p]) + delta;
        let cap = i128::from(net.places[p].capacity);
        if resulting > cap {
            overflows.push(PlaceOverflow {
                place: net.places[p].name.clone(),
                place_index: p,
                capacity: net.places[p].capacity,
                resulting: i64::try_from(resulting).map_err(|_| KernelError::ArithmeticOverflow)?,
            });
        }
    }

    let blocked = FireBlocked {
        insufficient_inputs: deficits,
        capacity_overflows: overflows,
    };
    Ok(blocked.is_blocked().then_some(blocked))
}

/// 变迁在给定标识下是否启用。
pub fn is_enabled(net: &Net, marking: &Marking, transition: TransitionId) -> bool {
    matches!(why_not_enabled(net, marking, transition), Ok(None))
}

/// 计算变迁发射后的新标识；前提不全部满足时返回阻断原因，原标识不被修改。
pub fn fire(
    net: &Net,
    marking: &Marking,
    transition: TransitionId,
) -> Result<Marking, FireError> {
    if let Some(blocked) = why_not_enabled(net, marking, transition).map_err(FireError::Invalid)? {
        return Err(FireError::Blocked(blocked));
    }
    let t = &net.transitions[transition];
    let mut next = marking.clone();
    for arc in &t.inputs {
        next.0[arc.place] -= arc.weight;
    }
    for arc in &t.outputs {
        next.0[arc.place] += arc.weight;
    }
    Ok(next)
}

/// 变迁的净增量向量 delta[p] = 输出权 - 输入权（i128 防溢出）。
pub(crate) fn net_change(net: &Net, t: &super::model::Transition) -> Result<Vec<i128>, KernelError> {
    let mut delta = vec![0i128; net.place_count()];
    for arc in &t.inputs {
        delta[arc.place] -= i128::from(arc.weight);
    }
    for arc in &t.outputs {
        delta[arc.place] += i128::from(arc.weight);
    }
    Ok(delta)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kernel::model::{ArcExpr, Marking, Net, Place, Transition};

    fn net_conservative() -> Net {
        Net {
            places: vec![
                Place { name: "p0".into(), capacity: 2 },
                Place { name: "p1".into(), capacity: 2 },
            ],
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![ArcExpr { place: 0, weight: 1 }],
                outputs: vec![ArcExpr { place: 1, weight: 1 }],
            }],
        }
    }

    #[test]
    fn enabled_requires_all_input_arcs_simultaneously() {
        let net = Net {
            places: vec![
                Place { name: "a".into(), capacity: 5 },
                Place { name: "b".into(), capacity: 5 },
            ],
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![
                    ArcExpr { place: 0, weight: 2 },
                    ArcExpr { place: 1, weight: 3 },
                ],
                outputs: vec![],
            }],
        };
        // 只满足一条弧：禁用，且必须同时报告两个缺口。
        let m = Marking(vec![2, 2]);
        let blocked = why_not_enabled(&net, &m, 0).unwrap().expect("must block");
        assert_eq!(blocked.insufficient_inputs.len(), 1);
        assert_eq!(blocked.insufficient_inputs[0].need, 3);
        assert_eq!(blocked.insufficient_inputs[0].have, 2);
        assert!(!is_enabled(&net, &m, 0));

        // 两条同时满足才启用。
        assert!(is_enabled(&net, &Marking(vec![2, 3]), 0));
    }

    #[test]
    fn capacity_overflow_blocks_and_does_not_truncate() {
        let net = Net {
            places: vec![Place { name: "p".into(), capacity: 3 }],
            transitions: vec![Transition {
                name: "add2".into(),
                inputs: vec![],
                outputs: vec![ArcExpr { place: 0, weight: 2 }],
            }],
        };
        // 2+2=4 > 3：禁止发射，原标识保持不变。
        let m = Marking(vec![2]);
        let err = fire(&net, &m, 0).expect_err("must be blocked");
        match err {
            FireError::Blocked(b) => {
                assert_eq!(b.capacity_overflows.len(), 1);
                assert_eq!(b.capacity_overflows[0].capacity, 3);
                assert_eq!(b.capacity_overflows[0].resulting, 4);
                assert!(b.insufficient_inputs.is_empty());
            }
            FireError::Invalid(e) => panic!("expected block, got invalid: {e}"),
        }
        assert_eq!(m.0, vec![2], "marking must be untouched on refusal");

        // 1+2=3：恰好到容量上界，合法。
        assert_eq!(fire(&net, &Marking(vec![1]), 0).unwrap().0, vec![3]);
    }

    #[test]
    fn atomic_consume_and_produce() {
        let net = net_conservative();
        let m = Marking(vec![1, 0]);
        let next = fire(&net, &m, 0).unwrap();
        assert_eq!(next.0, vec![0, 1]);
        assert_eq!(m.total_tokens(), next.total_tokens(), "total tokens conserved");
        // 再发射缺输入。
        let err = fire(&net, &next, 0).expect_err("no tokens left");
        match err {
            FireError::Blocked(b) => {
                assert_eq!(b.insufficient_inputs[0].place, "p0");
                assert!(b.capacity_overflows.is_empty());
            }
            _ => panic!("expected blocked"),
        }
    }

    #[test]
    fn weighted_arc_self_loop_respects_net_delta() {
        // t: 消耗 3 p、生成 5 p（净 +2）；容量 6：
        // 从 3 发射 -> 5 合法；从 5 发射 -> 7 容量拒绝；从 0 输入不足。
        let net = Net {
            places: vec![Place { name: "p".into(), capacity: 6 }],
            transitions: vec![Transition {
                name: "t".into(),
                inputs: vec![ArcExpr { place: 0, weight: 3 }],
                outputs: vec![ArcExpr { place: 0, weight: 5 }],
            }],
        };
        assert_eq!(fire(&net, &Marking(vec![3]), 0).unwrap().0, vec![5]);
        let b = why_not_enabled(&net, &Marking(vec![5]), 0).unwrap().unwrap();
        assert!(b.capacity_overflows.iter().any(|o| o.resulting == 7));
        assert!(b.insufficient_inputs.is_empty());
        let m0 = Marking(vec![0]);
        let b0 = why_not_enabled(&net, &m0, 0).unwrap().unwrap();
        assert_eq!(b0.insufficient_inputs.len(), 1);
        assert!(b0.capacity_overflows.is_empty());
    }

    #[test]
    fn bad_arguments_are_invalid_not_blocked() {
        let net = net_conservative();
        let m = Marking(vec![1]); // 长度不符
        let err = why_not_enabled(&net, &m, 0).unwrap_err();
        assert!(matches!(err, KernelError::MarkingLength { expected: 2, got: 1 }));
        let err2 = why_not_enabled(&net, &Marking(vec![1, 0]), 7).unwrap_err();
        assert!(matches!(err2, KernelError::UnknownTransition(_)));
    }
}
