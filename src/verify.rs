//! 独立证据检查器。
//!
//! 本模块**不依赖求解内核**：输入是规范化公式与证据（模型或消解推导记录），
//! 只用最直白的语义规则重放，因此内核即便有 bug 也无法“自证”。
//!
//! - SAT 模型：逐变量、逐子句检查；模型必须完整覆盖所有声明变量。
//! - UNSAT 推导：按 [`ResolutionProof`] 的步骤逐步重放消解，
//!   每一步校验枢轴文字在两侧各出现一次且极性相反，
//!   重放结果必须与声明的派生子句逐字一致，最终空子句引用也必须为真空。

use std::collections::{HashMap, HashSet};

use serde::Serialize;

use crate::cnf::{Formula, Lit};
use crate::evidence::ResolutionProof;

/// 模型验证失败类别（类别即结论，便于调用方断言具体失败原因）。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ModelError {
    /// 模型长度与声明变量数不符（缺失变量或越界）。
    Incomplete {
        expected_vars: usize,
        model_len: usize,
    },
    /// 某条子句没有任何文字被满足。
    ClauseUnsatisfied { clause_index: usize },
}

/// 推导记录验证失败类别。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ProofError {
    UnknownRef {
        ref_id: String,
    },
    DuplicateDerivedId {
        id: String,
    },
    /// 引用了排在后面的派生条目（证明必须顺序可验证）。
    ForwardReference {
        id: String,
        referenced: String,
    },
    EmptyDerivedId,
    BadPivot {
        at_derived: String,
        step: usize,
        pivot_var: u32,
        detail: &'static str,
    },
    /// 重放消解得到的子句与声明不符。
    ResolventMismatch {
        at_derived: String,
        step: usize,
        claimed: Vec<Lit>,
        recomputed: Vec<Lit>,
    },
    /// 声明的派生子句本身不规范（含重复或互补文字）。
    MalformedDerived {
        at_derived: String,
        detail: &'static str,
    },
    /// 被标记为空子句的引用并非空子句。
    EmptyRefNotEmpty {
        ref_id: String,
        actual_len: usize,
    },
    /// 消解引用了空子句（除最终引用外，空子句不能再参与消解——否则可推出任意结论）。
    ResolvedWithEmpty {
        at_derived: String,
        step: usize,
        ref_id: String,
    },
}

/// 检查完整模型：模型为完整真值向量，下标 i 即变量 i 的真假（0 号位忽略）。
pub fn check_model(formula: &Formula, model: &[bool]) -> Result<(), ModelError> {
    if model.len() != formula.num_vars + 1 {
        return Err(ModelError::Incomplete {
            expected_vars: formula.num_vars,
            model_len: model.len().saturating_sub(1),
        });
    }
    let lit_true = |l: Lit| -> bool {
        let v = l.unsigned_abs() as usize;
        if l > 0 {
            model[v]
        } else {
            !model[v]
        }
    };
    for (i, c) in formula.clauses.iter().enumerate() {
        if !c.lits.iter().any(|&l| lit_true(l)) {
            return Err(ModelError::ClauseUnsatisfied { clause_index: i });
        }
    }
    Ok(())
}

/// 规范化形式的子句（与 [`crate::cnf::normalize_clause`] 同规则），检查器自己实现一份。
fn sorted_dedup(lits: &[Lit]) -> Vec<Lit> {
    let mut v: Vec<Lit> = lits.to_vec();
    v.sort_by_key(|l| (l.unsigned_abs(), *l < 0));
    v.dedup();
    v
}

/// 检查 UNSAT 推导记录。
pub fn check_proof(formula: &Formula, proof: &ResolutionProof) -> Result<(), ProofError> {
    // 1) 引用解析表：输入子句 i<n> 与顺序累积的派生 d<n>。
    let mut known: HashMap<String, Vec<Lit>> = HashMap::new();
    for (i, c) in formula.clauses.iter().enumerate() {
        known.insert(format!("i{i}"), c.lits.clone());
    }
    for d in &proof.derived_clauses {
        if d.id.is_empty() {
            return Err(ProofError::EmptyDerivedId);
        }
        if known.contains_key(&d.id) {
            return Err(ProofError::DuplicateDerivedId { id: d.id.clone() });
        }
        // 占位：存在性先登记，值在重放成功后写入（同 id 前向引用由上面一行挡住）。
        known.insert(d.id.clone(), Vec::new());
    }

    // 2) 逐条重放。
    for (idx, d) in proof.derived_clauses.iter().enumerate() {
        let claimed = sorted_dedup(&d.literals);
        // 声明子句自身的规范性：不能有互补对。
        let vars: HashSet<u32> = claimed.iter().map(|l| l.unsigned_abs()).collect();
        if vars.len() != claimed.len() {
            return Err(ProofError::MalformedDerived {
                at_derived: d.id.clone(),
                detail: "complementary literal pair in derived clause",
            });
        }

        let mut cur = lookup_ref(
            &known,
            formula,
            &d.start_ref,
            idx,
            &d.id,
            &proof.derived_clauses,
        )?
        .clone();

        for (step, op) in d.resolvents.iter().enumerate() {
            let other = lookup_ref(
                &known,
                formula,
                &op.with_ref,
                idx,
                &d.id,
                &proof.derived_clauses,
            )?;
            if other.is_empty() {
                return Err(ProofError::ResolvedWithEmpty {
                    at_derived: d.id.clone(),
                    step,
                    ref_id: op.with_ref.clone(),
                });
            }
            if cur.is_empty() {
                return Err(ProofError::ResolvedWithEmpty {
                    at_derived: d.id.clone(),
                    step,
                    ref_id: d.start_ref.clone(),
                });
            }
            let pivot = op.pivot_var;
            let pos = pivot as Lit;
            let neg = -(pivot as Lit);
            let in_cur_pos = cur.contains(&pos);
            let in_cur_neg = cur.contains(&neg);
            let in_oth_pos = other.contains(&pos);
            let in_oth_neg = other.contains(&neg);
            if !(in_cur_pos ^ in_cur_neg) || !(in_oth_pos ^ in_oth_neg) {
                return Err(ProofError::BadPivot {
                    at_derived: d.id.clone(),
                    step,
                    pivot_var: pivot,
                    detail: "pivot variable must occur exactly once, with one polarity, in each resolvent",
                });
            }
            if in_cur_pos == in_oth_pos {
                return Err(ProofError::BadPivot {
                    at_derived: d.id.clone(),
                    step,
                    pivot_var: pivot,
                    detail: "pivot literals must have opposite polarities",
                });
            }
            // 标准消解：删去两侧枢轴文字，合并其余文字（重言式自然由规范化暴露）。
            let mut merged: Vec<Lit> = cur
                .iter()
                .chain(other.iter())
                .copied()
                .filter(|l| l.unsigned_abs() != pivot)
                .collect();
            merged = sorted_dedup(&merged);
            cur = merged;
        }

        if cur != claimed {
            return Err(ProofError::ResolventMismatch {
                at_derived: d.id.clone(),
                step: d.resolvents.len().saturating_sub(1),
                claimed: claimed.clone(),
                recomputed: cur.clone(),
            });
        }
        known.insert(d.id.clone(), cur);
    }

    // 3) 空子句引用必须真实为空。
    let final_lits = lookup_ref(
        &known,
        formula,
        &proof.empty_clause_ref,
        proof.derived_clauses.len(),
        &proof.empty_clause_ref,
        &proof.derived_clauses,
    )?;
    if !final_lits.is_empty() {
        return Err(ProofError::EmptyRefNotEmpty {
            ref_id: proof.empty_clause_ref.clone(),
            actual_len: final_lits.len(),
        });
    }
    Ok(())
}

/// 解析 `i<n>` / `d<n>` 引用，并拦截前向引用。
#[allow(clippy::too_many_arguments)]
fn lookup_ref<'a>(
    known: &'a HashMap<String, Vec<Lit>>,
    formula: &Formula,
    ref_id: &str,
    current_idx: usize,
    current_id: &str,
    all_derived: &[crate::evidence::DerivedClause],
) -> Result<&'a Vec<Lit>, ProofError> {
    if let Some(id) = ref_id.strip_prefix('d') {
        let di: usize = id.parse().map_err(|_| ProofError::UnknownRef {
            ref_id: ref_id.to_string(),
        })?;
        if di >= all_derived.len() || all_derived[di].id != ref_id {
            return Err(ProofError::UnknownRef {
                ref_id: ref_id.to_string(),
            });
        }
        if di >= current_idx {
            return Err(ProofError::ForwardReference {
                id: current_id.to_string(),
                referenced: ref_id.to_string(),
            });
        }
    } else if let Some(idx_str) = ref_id.strip_prefix('i') {
        let idx: usize = idx_str.parse().map_err(|_| ProofError::UnknownRef {
            ref_id: ref_id.to_string(),
        })?;
        if idx >= formula.clauses.len() {
            return Err(ProofError::UnknownRef {
                ref_id: ref_id.to_string(),
            });
        }
    } else {
        return Err(ProofError::UnknownRef {
            ref_id: ref_id.to_string(),
        });
    }
    known.get(ref_id).ok_or_else(|| ProofError::UnknownRef {
        ref_id: ref_id.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cnf::normalize_formula;

    #[test]
    fn accepts_and_rejects_models_by_clause() {
        let (f, _) = normalize_formula(None, &[vec![1, -2], vec![2]]).unwrap();
        assert!(check_model(&f, &[false, true, true]).is_ok());
        let err = check_model(&f, &[false, false, false]).unwrap_err();
        assert_eq!(err, ModelError::ClauseUnsatisfied { clause_index: 1 });
        assert!(matches!(
            check_model(&f, &[false, true]),
            Err(ModelError::Incomplete {
                expected_vars: 2,
                model_len: 1
            })
        ));
    }

    #[test]
    fn accepts_valid_resolution_proof() {
        // (x1) 与 (¬x1) 直接消解为空。
        let (f, _) = normalize_formula(None, &[vec![1], vec![-1]]).unwrap();
        let proof = ResolutionProof {
            derived_clauses: vec![crate::evidence::DerivedClause {
                id: "d0".into(),
                literals: vec![],
                start_ref: "i0".into(),
                resolvents: vec![crate::evidence::ResolventOp {
                    pivot_var: 1,
                    with_ref: "i1".into(),
                }],
            }],
            empty_clause_ref: "d0".into(),
        };
        assert!(check_proof(&f, &proof).is_ok());
    }

    #[test]
    fn rejects_same_polarity_pivot() {
        // 两条子句都含同极性枢轴 x1，记录却声称消解为空：枢轴极性检查必须拒绝。
        let (f, _) = normalize_formula(None, &[vec![1], vec![1]]).unwrap();
        let proof = ResolutionProof {
            derived_clauses: vec![crate::evidence::DerivedClause {
                id: "d0".into(),
                literals: vec![],
                start_ref: "i0".into(),
                resolvents: vec![crate::evidence::ResolventOp {
                    pivot_var: 1,
                    with_ref: "i1".into(),
                }],
            }],
            empty_clause_ref: "d0".into(),
        };
        assert!(matches!(
            check_proof(&f, &proof),
            Err(ProofError::BadPivot { .. })
        ));
    }

    #[test]
    fn rejects_resolvent_mismatch_after_tampering() {
        // 合法消解结果是空子句，但记录谎称只推出了 (x2)：必须按 ResolventMismatch 拒绝。
        let (f, _) = normalize_formula(None, &[vec![1, 2], vec![-1]]).unwrap();
        let proof = ResolutionProof {
            derived_clauses: vec![crate::evidence::DerivedClause {
                id: "d0".into(),
                literals: vec![2, 3], // 篡改：真实消解结果只有 x2
                start_ref: "i0".into(),
                resolvents: vec![crate::evidence::ResolventOp {
                    pivot_var: 1,
                    with_ref: "i1".into(),
                }],
            }],
            empty_clause_ref: "d0".into(),
        };
        assert!(matches!(
            check_proof(&f, &proof),
            Err(ProofError::ResolventMismatch { .. })
        ));
    }

    #[test]
    fn rejects_tampered_empty_ref() {
        let (f, _) = normalize_formula(None, &[vec![1], vec![-1]]).unwrap();
        let proof = ResolutionProof {
            derived_clauses: vec![],
            empty_clause_ref: "i0".into(), // i0 是单位子句，不是空的
        };
        assert_eq!(
            check_proof(&f, &proof).unwrap_err(),
            ProofError::EmptyRefNotEmpty {
                ref_id: "i0".into(),
                actual_len: 1
            }
        );
    }

    #[test]
    fn rejects_unknown_and_forward_refs() {
        let (f, _) = normalize_formula(None, &[vec![1], vec![-1]]).unwrap();
        let proof = ResolutionProof {
            derived_clauses: vec![crate::evidence::DerivedClause {
                id: "d0".into(),
                literals: vec![],
                start_ref: "i9".into(),
                resolvents: vec![],
            }],
            empty_clause_ref: "d0".into(),
        };
        assert!(matches!(
            check_proof(&f, &proof),
            Err(ProofError::UnknownRef { .. })
        ));
    }
}
