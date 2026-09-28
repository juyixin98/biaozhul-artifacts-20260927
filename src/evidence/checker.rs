//! 独立证据检查器。
//!
//! 独立性约束：本模块**不调用** `crate::normalize`，而是自己重新实现一份
//! 规范化（去重 / 去重言式 / 排序），并用自己的归结规则校验证明。这样即使
//! 求解内核与规范器同源出错，检查器仍能独立发现问题。验收测试中的暴力
//! 真值表 oracle（`tests/common/oracle.rs`）是第三份独立实现。

use std::collections::{HashMap, HashSet};

use crate::evidence::types::{
    ClauseRef, Model, Outcome, ResolutionProof,
};
use crate::lit::{lit_from_signed, neg, var_of, Lit};

/// 模型检查失败类别。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ModelError {
    /// 模型给变量 v 赋了超出公式变量范围的值。
    VarOutOfRange { var: u32, num_vars: usize },
    /// 模型里同一变量出现了多次（同极性重复）。
    DuplicateVar { var: u32 },
    /// 模型里同一变量正负极性同时出现。
    ConflictingVar { var: u32 },
    /// 模型缺少变量 v 的赋值。
    MissingVar { var: u32 },
    /// 证据自报的 num_vars 与公式实际有效变量数不一致。
    NumVarsMismatch { reported: usize, actual: usize },
    /// 第 idx 条规范化子句不被模型满足。
    ClauseNotSatisfied { idx: usize, clause: Vec<Lit> },
}

impl std::fmt::Display for ModelError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ModelError::VarOutOfRange { var, num_vars } => {
                write!(f, "模型引用变量 {var}，超出公式变量数 {num_vars}")
            }
            ModelError::DuplicateVar { var } => {
                write!(f, "模型中变量 {var} 重复出现")
            }
            ModelError::ConflictingVar { var } => {
                write!(f, "模型对变量 {var} 同时给出正负赋值")
            }
            ModelError::MissingVar { var } => {
                write!(f, "模型缺少变量 {var} 的赋值")
            }
            ModelError::NumVarsMismatch { reported, actual } => write!(
                f,
                "证据自报 num_vars={reported}，与公式实际有效变量数 {actual} 不一致"
            ),
            ModelError::ClauseNotSatisfied { idx, clause } => write!(
                f,
                "第 {idx} 条子句不被模型满足: {:?}",
                clause
                    .iter()
                    .map(|&l| crate::lit::lit_to_signed(l))
                    .collect::<Vec<_>>()
            ),
        }
    }
}

impl std::error::Error for ModelError {}

/// 证明检查失败类别。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ProofError {
    /// 证明不含任何推导步，却声称推出空子句。
    EmptyProof,
    /// 第 step 步的编号不等于其位置（必须从 0 连续递增）。
    IdNotSequential { step: usize, got: u64 },
    /// 引用了越界的原始子句编号。
    BadInputRef { step: u64, idx: usize, len: usize },
    /// 引用了尚未定义（或不存在）的引理。
    BadLemmaRef { step: u64, id: u64 },
    /// 第 step 步第 at 次归结：两个子句间不存在可消元的互补对。
    NoPivot { step: u64, at: usize },
    /// 枢轴记录数与归结次数不一致。
    PivotCountMismatch { step: u64, sides: usize, pivots: usize },
    /// 第 step 步第 at 次归结：存在多个互补对（本格式要求唯一枢轴）。
    MultiplePivots {
        step: u64,
        at: usize,
        pivots: Vec<u32>,
    },
    /// 记录的枢轴变量与实际互补对不一致。
    PivotMismatch {
        step: u64,
        at: usize,
        declared: u32,
        actual: u32,
    },
    /// 声明的 resolvent 与逐次归结实际得到的子句不同。
    ResolventMismatch {
        step: u64,
        declared: Vec<Lit>,
        computed: Vec<Lit>,
    },
    /// 最后一步推出的不是空子句。
    FinalNotEmpty { last: Vec<Lit> },
}

impl std::fmt::Display for ProofError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ProofError::EmptyProof => write!(f, "证明为空，无法推出空子句"),
            ProofError::IdNotSequential { step, got } => write!(
                f,
                "第 {step} 步编号为 {got}，应为 {step}（从 0 连续递增）"
            ),
            ProofError::BadInputRef { step, idx, len } => write!(
                f,
                "步骤 {step} 引用输入子句 {idx}，但公式只有 {len} 条规范化子句"
            ),
            ProofError::BadLemmaRef { step, id } => {
                write!(f, "步骤 {step} 前向或非法引用引理 {id}")
            }
            ProofError::NoPivot { step, at } => write!(
                f,
                "步骤 {step} 第 {at} 次归结缺少互补枢轴文字"
            ),
            ProofError::PivotCountMismatch {
                step,
                sides,
                pivots,
            } => write!(
                f,
                "步骤 {step} 记录了 {pivots} 个枢轴却有 {sides} 次归结"
            ),
            ProofError::MultiplePivots { step, at, pivots } => write!(
                f,
                "步骤 {step} 第 {at} 次归结存在多个互补枢轴变量 {pivots:?}"
            ),
            ProofError::PivotMismatch {
                step,
                at,
                declared,
                actual,
            } => write!(
                f,
                "步骤 {step} 第 {at} 次归结枢轴不一致：记录 {declared}，实际 {actual}"
            ),
            ProofError::ResolventMismatch {
                step,
                declared,
                computed,
            } => write!(
                f,
                "步骤 {step} 的 resolvent 被篡改或计算错误：记录 {:?}，实际归结得 {:?}",
                signed(declared),
                signed(computed)
            ),
            ProofError::FinalNotEmpty { last } => write!(
                f,
                "最后一步推出 {:?} 而非空子句，UNSAT 不成立",
                signed(last)
            ),
        }
    }
}

fn signed(lits: &[Lit]) -> Vec<i64> {
    lits.iter()
        .map(|&l| crate::lit::lit_to_signed(l))
        .collect()
}

impl std::error::Error for ProofError {}

/// 检查器自带的规范化：独立实现，刻意不与 `crate::normalize` 共享代码。
/// 返回（规范化子句, 有效变量数）。
pub fn independent_canonicalize(
    declared_vars: usize,
    raw: &[Vec<i64>],
) -> (Vec<Vec<Lit>>, usize) {
    let mut out: Vec<Vec<Lit>> = Vec::with_capacity(raw.len());
    let mut max_var: u32 = 0;
    for clause in raw {
        if clause.is_empty() {
            out.push(Vec::new());
            continue;
        }
        let mut uniq: HashSet<Lit> = HashSet::new();
        let mut clash = false;
        for &s in clause {
            let l = lit_from_signed(s);
            max_var = max_var.max(var_of(l));
            if uniq.contains(&neg(l)) {
                clash = true;
            }
            uniq.insert(l);
        }
        if clash {
            continue;
        }
        let mut v: Vec<Lit> = uniq.into_iter().collect();
        v.sort_unstable();
        out.push(v);
    }
    let nvars = declared_vars.max(max_var as usize);
    (out, nvars)
}

/// 检查 SAT 模型。`raw_clauses` 是未经规范化的原始带符号子句。
pub fn check_model(
    declared_vars: usize,
    raw_clauses: &[Vec<i64>],
    model: &Model,
) -> Result<(), ModelError> {
    let (clauses, nvars) = independent_canonicalize(declared_vars, raw_clauses);

    // 1) 模型必须是对 1..=nvars 每个变量恰好一个极性的全赋值。
    let mut assigned: HashSet<u32> = HashSet::new();
    for &lit in &model.true_literals {
        let v = var_of(lit);
        if v as usize > nvars || v == 0 {
            return Err(ModelError::VarOutOfRange {
                var: v,
                num_vars: nvars,
            });
        }
        if assigned.contains(&v) {
            let pos_present = model
                .true_literals
                .iter()
                .any(|&x| var_of(x) == v && (x & 1 == 0));
            let neg_present = model
                .true_literals
                .iter()
                .any(|&x| var_of(x) == v && (x & 1 == 1));
            return if pos_present && neg_present {
                Err(ModelError::ConflictingVar { var: v })
            } else {
                Err(ModelError::DuplicateVar { var: v })
            };
        }
        assigned.insert(v);
    }
    for v in 1..=nvars as u32 {
        if !assigned.contains(&v) {
            return Err(ModelError::MissingVar { var: v });
        }
    }
    if model.num_vars != nvars {
        return Err(ModelError::NumVarsMismatch {
            reported: model.num_vars,
            actual: nvars,
        });
    }

    // 2) 真值集合：每条子句至少一个文字为真。
    let trues: HashSet<Lit> = model.true_literals.iter().copied().collect();
    for (idx, clause) in clauses.iter().enumerate() {
        if !clause.iter().any(|l| trues.contains(l)) {
            return Err(ModelError::ClauseNotSatisfied {
                idx,
                clause: clause.clone(),
            });
        }
    }
    Ok(())
}

/// 单个二元归结：返回消去唯一互补对后的子句（已排序去重）与枢轴变量。
/// 互补对为 0 个时返回空 Vec 错误；多于 1 个时返回枢轴变量列表。
fn resolve_once(a: &[Lit], b: &[Lit]) -> Result<(Vec<Lit>, u32), Vec<u32>> {
    let sa: HashSet<Lit> = a.iter().copied().collect();
    let sb: HashSet<Lit> = b.iter().copied().collect();
    let mut pivots: Vec<u32> = Vec::new();
    for &l in &sa {
        if sb.contains(&neg(l)) {
            let v = var_of(l);
            if !pivots.contains(&v) {
                pivots.push(v);
            }
        }
    }
    if pivots.is_empty() {
        return Err(Vec::new());
    }
    if pivots.len() > 1 {
        pivots.sort_unstable();
        return Err(pivots);
    }
    let pivot = pivots[0];
    let mut merged: Vec<Lit> = sa
        .iter()
        .chain(sb.iter())
        .copied()
        .filter(|l| var_of(*l) != pivot)
        .collect();
    merged.sort_unstable();
    merged.dedup();
    Ok((merged, pivot))
}

/// 检查 UNSAT 归结证明。
pub fn check_proof(
    declared_vars: usize,
    raw_clauses: &[Vec<i64>],
    proof: &ResolutionProof,
) -> Result<(), ProofError> {
    let (input_clauses, _nvars) =
        independent_canonicalize(declared_vars, raw_clauses);

    if proof.steps.is_empty() {
        // 空证明无法表达任何推导，拒绝（含空子句的输入也必须显式给出一步）。
        return Err(ProofError::EmptyProof);
    }

    let mut lemmas: HashMap<u64, Vec<Lit>> = HashMap::new();

    for (pos, step) in proof.steps.iter().enumerate() {
        if step.id as usize != pos {
            return Err(ProofError::IdNotSequential {
                step: pos,
                got: step.id,
            });
        }
        let lookup = |r: &ClauseRef| -> Result<Vec<Lit>, ProofError> {
            match r {
                ClauseRef::Input { idx } => input_clauses
                    .get(*idx)
                    .cloned()
                    .ok_or(ProofError::BadInputRef {
                        step: step.id,
                        idx: *idx,
                        len: input_clauses.len(),
                    }),
                ClauseRef::Lemma { id } => {
                    lemmas.get(id).cloned().ok_or(ProofError::BadLemmaRef {
                        step: step.id,
                        id: *id,
                    })
                }
            }
        };

        if step.pivot_vars.len() != step.side.len() {
            return Err(ProofError::PivotCountMismatch {
                step: step.id,
                sides: step.side.len(),
                pivots: step.pivot_vars.len(),
            });
        }

        let mut current = lookup(&step.main)?;
        for (at, side_ref) in step.side.iter().enumerate() {
            let side = lookup(side_ref)?;
            match resolve_once(&current, &side) {
                Ok((next, actual_pivot)) => {
                    if step.pivot_vars[at] != actual_pivot {
                        return Err(ProofError::PivotMismatch {
                            step: step.id,
                            at,
                            declared: step.pivot_vars[at],
                            actual: actual_pivot,
                        });
                    }
                    current = next;
                }
                Err(pivots) => {
                    if pivots.is_empty() {
                        return Err(ProofError::NoPivot {
                            step: step.id,
                            at,
                        });
                    } else {
                        return Err(ProofError::MultiplePivots {
                            step: step.id,
                            at,
                            pivots,
                        });
                    }
                }
            }
        }
        current.sort_unstable();
        current.dedup();
        if current != step.resolvent {
            return Err(ProofError::ResolventMismatch {
                step: step.id,
                declared: step.resolvent.clone(),
                computed: current,
            });
        }
        lemmas.insert(step.id, step.resolvent.clone());
    }

    let last = &proof.steps.last().unwrap().resolvent;
    if !last.is_empty() {
        return Err(ProofError::FinalNotEmpty {
            last: last.clone(),
        });
    }
    Ok(())
}

/// 按结论分派检查：SAT 验模型，UNSAT 验证明，UNKNOWN 不可被背书。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OutcomeCheckError {
    UnknownCannotBeVerified,
    Model(ModelError),
    Proof(ProofError),
}

impl std::fmt::Display for OutcomeCheckError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OutcomeCheckError::UnknownCannotBeVerified => {
                write!(f, "UNKNOWN 不携带可证实结论，检查器不予背书")
            }
            OutcomeCheckError::Model(m) => write!(f, "{m}"),
            OutcomeCheckError::Proof(p) => write!(f, "{p}"),
        }
    }
}

impl std::error::Error for OutcomeCheckError {}

pub fn check_outcome(
    declared_vars: usize,
    raw_clauses: &[Vec<i64>],
    outcome: &Outcome,
) -> Result<(), OutcomeCheckError> {
    match outcome {
        Outcome::Sat { model } => {
            check_model(declared_vars, raw_clauses, model)
                .map_err(OutcomeCheckError::Model)
        }
        Outcome::Unsat { proof } => {
            check_proof(declared_vars, raw_clauses, proof)
                .map_err(OutcomeCheckError::Proof)
        }
        Outcome::Unknown { .. } => {
            Err(OutcomeCheckError::UnknownCannotBeVerified)
        }
    }
}
