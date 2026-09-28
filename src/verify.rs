//! 证据验证模块。
//!
//! 这里的判定**不**以被测内核的结论为标准答案：
//! - [`all_assignments`] / [`eval_truth_table`] 用 [`Expr::eval`] 直接递归解释器
//!   枚举真值表，是与 ROBDD 完全独立的参考实现；
//! - [`equiv_exprs`] 用该真值表判定两个表达式在给定**变量身份映射**下是否等价，
//!   并在不等价时给出见证赋值；
//! - [`equiv_edges`] 额外给出内核结论（规范边相等）与独立真值表结论，
//!   供后端交叉比对——两者不一致时内核有 bug。
//!
//! 变量身份：等价结论绑定“变量身份”。两个管理器里的名字默认是不同身份；
//! 只有通过 [`IdentityMapping`] 显式声明的名字对才共享同一身份。
//! 映射必须是双射；存在未绑定的共享身份变量时返回“无法判定”而不是猜测。

use std::collections::{HashMap, HashSet};

use serde::{Deserialize, Serialize};

use crate::core::{BddError, BddManager, Edge, VarId};
use crate::lang::Expr;

/// 穷举真值表的安全上限（2^20 个赋值）。
pub const DEFAULT_VAR_CAP: usize = 20;

/// 按 `vars` 顺序枚举全部赋值。第 k 个赋值中变量 i 的值为 `k` 的第 i 位。
pub fn all_assignments(vars: &[String], cap: usize) -> Result<Vec<Vec<bool>>, BddError> {
    if vars.len() > cap {
        return Err(BddError::TruthTableTooLarge {
            variables: vars.len(),
            limit: cap,
        });
    }
    let n = vars.len();
    let count = 1usize << n;
    Ok((0..count)
        .map(|k| (0..n).map(|i| (k >> i) & 1 == 1).collect())
        .collect())
}

/// 用独立解释器求表达式在每个赋值上的值。
pub fn eval_truth_table(expr: &Expr, vars: &[String], cap: usize) -> Result<Vec<bool>, BddError> {
    Ok(all_assignments(vars, cap)?
        .iter()
        .map(|bits| {
            let env: HashMap<String, bool> =
                vars.iter().cloned().zip(bits.iter().copied()).collect();
            expr.eval(&env)
        })
        .collect())
}

/// 变量身份映射：`left_name -> right_name`，每一侧的名字至多出现一次。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(transparent)]
pub struct IdentityMapping {
    pub pairs: HashMap<String, String>,
}

/// 映射本身不合法（与函数取值无关的拒绝类原因）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MappingError {
    /// 左侧同一个名字被映射两次。
    LeftNotUnique(String),
    /// 右侧同一个名字被两个左侧名字指向。
    RightNotUnique(String),
    /// 左右各有未被映射覆盖的变量，它们的身份无法对齐。
    UnboundVariables {
        left: Vec<String>,
        right: Vec<String>,
    },
}

impl std::fmt::Display for MappingError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MappingError::LeftNotUnique(n) => write!(f, "left variable {n:?} mapped more than once"),
            MappingError::RightNotUnique(n) => write!(f, "right variable {n:?} is the image of multiple left variables"),
            MappingError::UnboundVariables { left, right } => write!(
                f,
                "unbound variables prevent a bijective identity map (left={left:?}, right={right:?})"
            ),
        }
    }
}

impl IdentityMapping {
    pub fn new(pairs: HashMap<String, String>) -> Self {
        Self { pairs }
    }

    /// 校验并规范化映射。
    ///
    /// `left_vars`/`right_vars` 是两边实际出现的变量集合。
    /// - 双射检查（同名两边都出现时自动视为恒等对，可省略）；
    /// - 所有出现的变量都必须被身份覆盖，否则无法做完整真值表对齐。
    pub fn validate(
        &self,
        left_vars: &HashSet<String>,
        right_vars: &HashSet<String>,
    ) -> Result<Vec<(String, String)>, MappingError> {
        // 同名变量自动配对，但显式映射不得与该恒等关系矛盾。
        let mut pairs: Vec<(String, String)> = Vec::new();
        let mut used_left: HashSet<String> = HashSet::new();
        let mut used_right: HashSet<String> = HashSet::new();

        for (l, r) in &self.pairs {
            if !used_left.insert(l.clone()) {
                return Err(MappingError::LeftNotUnique(l.clone()));
            }
            if !used_right.insert(r.clone()) {
                return Err(MappingError::RightNotUnique(r.clone()));
            }
            pairs.push((l.clone(), r.clone()));
        }
        // 同名恒等对补齐。
        for name in left_vars.iter().filter(|n| right_vars.contains(*n)) {
            if !used_left.contains(name) && !used_right.contains(name) {
                used_left.insert(name.clone());
                used_right.insert(name.clone());
                pairs.push((name.clone(), name.clone()));
            } else if used_left.contains(name) {
                // 显式把同名变量映射到别处是允许的（重命名情形）；
                // 但若右边同名又被别的左变量占用，则双射已在上面拒绝。
            }
        }

        let unbound_left: Vec<String> = left_vars.difference(&used_left).cloned().collect();
        let unbound_right: Vec<String> = right_vars.difference(&used_right).cloned().collect();
        if !unbound_left.is_empty() || !unbound_right.is_empty() {
            let mut l = unbound_left;
            let mut r = unbound_right;
            l.sort();
            r.sort();
            return Err(MappingError::UnboundVariables { left: l, right: r });
        }
        pairs.sort();
        Ok(pairs)
    }
}

/// 见证：按身份变量序给出的赋值，以及两边各自的取值。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Witness {
    pub assignment: HashMap<String, bool>,
    pub left_value: bool,
    pub right_value: bool,
}

/// 等价判定结果。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(tag = "verdict", rename_all = "snake_case")]
pub enum EquivResult {
    /// 独立真值表确认等价。
    Equivalent {
        identities: Vec<String>,
        assignments_checked: usize,
    },
    /// 独立真值表找到反例。
    NotEquivalent {
        identities: Vec<String>,
        witness: Witness,
    },
}

/// 用独立真值表判定两个表达式在身份映射下是否等价。
///
/// 身份变量序：左侧变量名按字母序；右侧表达式按映射重命名到左侧身份空间后求值。
pub fn equiv_exprs(
    left: &Expr,
    right: &Expr,
    mapping: &IdentityMapping,
    cap: usize,
) -> Result<EquivResult, BddError> {
    let left_set: HashSet<String> = left.vars().into_iter().collect();
    let right_set: HashSet<String> = right.vars().into_iter().collect();
    let pairs = mapping
        .validate(&left_set, &right_set)
        .map_err(|e| BddError::MappingRejected(e.to_string()))?;

    // 身份空间采用左侧名字；right→left 反向重命名。
    let mut identities: Vec<String> = pairs.iter().map(|(l, _)| l.clone()).collect();
    identities.sort();
    identities.dedup();

    let reverse: HashMap<String, String> = pairs.into_iter().map(|(l, r)| (r, l)).collect();
    let right_renamed = right.rename(&reverse);

    for bits in all_assignments(&identities, cap)? {
        let env: HashMap<String, bool> = identities
            .iter()
            .cloned()
            .zip(bits.iter().copied())
            .collect();
        let lv = left.eval(&env);
        let rv = right_renamed.eval(&env);
        if lv != rv {
            return Ok(EquivResult::NotEquivalent {
                identities,
                witness: Witness {
                    assignment: env,
                    left_value: lv,
                    right_value: rv,
                },
            });
        }
    }
    Ok(EquivResult::Equivalent {
        assignments_checked: 1usize << identities.len(),
        identities,
    })
}

/// 同管理器内两条边的等价性：规范边相等（内核结论）+ 独立真值表（参考结论）。
///
/// `vars` 为参与判定的身份变量序（通常即管理器变量序的子集/全集）。
/// 返回三元组：(规范边是否相等, 独立真值表是否等价, 见证或赋值数)。
pub fn equiv_edges(
    manager: &BddManager,
    a: Edge,
    b: Edge,
    vars: &[String],
    cap: usize,
) -> Result<EdgeEquivReport, BddError> {
    a.require_owner(manager.id())?;
    b.require_owner(manager.id())?;

    let canonical_equal = a == b;
    let assignments = all_assignments(vars, cap)?;

    let var_ids: Vec<VarId> = vars
        .iter()
        .map(|name| {
            manager
                .var_id(name)
                .ok_or_else(|| BddError::UnknownVariable(name.clone()))
        })
        .collect::<Result<_, _>>()?;

    let mut witness: Option<Witness> = None;
    for bits in &assignments {
        let mut ordered = vec![false; manager.variable_order().len()];
        for (vid, bit) in var_ids.iter().zip(bits) {
            ordered[vid.0 as usize] = *bit;
        }
        let va = manager.evaluate(a, &ordered)?;
        let vb = manager.evaluate(b, &ordered)?;
        if va != vb {
            witness = Some(Witness {
                assignment: vars.iter().cloned().zip(bits.iter().copied()).collect(),
                left_value: va,
                right_value: vb,
            });
            break;
        }
    }

    Ok(EdgeEquivReport {
        canonical_equal,
        truth_table_equivalent: witness.is_none(),
        assignments_checked: assignments.len(),
        witness,
    })
}

/// [`equiv_edges`] 的详细报告。
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EdgeEquivReport {
    /// 内核结论：两条边是否为同一条规范边。
    pub canonical_equal: bool,
    /// 独立解释器遍历真值表的结论。
    pub truth_table_equivalent: bool,
    pub assignments_checked: usize,
    pub witness: Option<Witness>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::lang::parser::parse;

    fn set(items: &[&str]) -> HashSet<String> {
        items.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn assignment_count_is_power_of_two_in_variable_order() {
        let vars = vec!["a".to_string(), "b".to_string()];
        let asg = all_assignments(&vars, DEFAULT_VAR_CAP).unwrap();
        assert_eq!(asg.len(), 4);
        assert_eq!(asg[0], vec![false, false]);
        assert_eq!(asg[3], vec![true, true]);
    }

    #[test]
    fn different_syntax_same_function_is_equivalent_with_witness_on_mismatch() {
        let l = parse("a & (b | c)").unwrap();
        let r = parse("(a & b) | (a & c)").unwrap();
        let mapping = IdentityMapping::default(); // 全部同名
        let out = equiv_exprs(&l, &r, &mapping, DEFAULT_VAR_CAP).unwrap();
        match out {
            EquivResult::Equivalent {
                assignments_checked,
                ..
            } => assert_eq!(assignments_checked, 8),
            other => panic!("expected equivalent, got {other:?}"),
        }

        let bad = parse("a | (b & c)").unwrap();
        let out = equiv_exprs(&l, &bad, &mapping, DEFAULT_VAR_CAP).unwrap();
        match out {
            EquivResult::NotEquivalent { witness, .. } => {
                // 枚举从 k=0 开始逐位赋值，首个分歧出现在 a=1,b=0,c=0：
                // 左 a&(b|c)=0，右 a|(b&c)=1。
                assert!(witness.assignment["a"]);
                assert!(!(witness.assignment["b"]));
                assert!(!(witness.assignment["c"]));
                assert!(!(witness.left_value));
                assert!(witness.right_value);
            }
            other => panic!("expected witness, got {other:?}"),
        }
    }

    #[test]
    fn renaming_across_managers_equivalent_via_identity_mapping() {
        let l = parse("x & y -> z").unwrap();
        let r = parse("p & q -> r").unwrap();
        let mapping = IdentityMapping::new(HashMap::from([
            ("x".to_string(), "p".to_string()),
            ("y".to_string(), "q".to_string()),
            ("z".to_string(), "r".to_string()),
        ]));
        assert!(matches!(
            equiv_exprs(&l, &r, &mapping, DEFAULT_VAR_CAP).unwrap(),
            EquivResult::Equivalent {
                assignments_checked: 8,
                ..
            }
        ));

        // 非双射：右侧多一个未绑定变量 → 拒绝。
        let r2 = parse("p & q -> r | s").unwrap();
        let err = equiv_exprs(&l, &r2, &mapping, DEFAULT_VAR_CAP).unwrap_err();
        assert!(matches!(err, BddError::MappingRejected(_)));
    }

    #[test]
    fn non_bijective_mapping_is_rejected() {
        let mapping = IdentityMapping::new(HashMap::from([
            ("a".to_string(), "x".to_string()),
            ("b".to_string(), "x".to_string()),
        ]));
        let err = mapping
            .validate(&set(&["a", "b"]), &set(&["x"]))
            .unwrap_err();
        assert!(matches!(err, MappingError::RightNotUnique(_)));
    }

    #[test]
    fn edge_equiv_agrees_kernel_and_oracle_and_catches_disagreement() {
        let mut m = BddManager::new(&["a".into(), "b".into()]).unwrap();
        let a = m.build(&parse("a | b").unwrap()).unwrap();
        let b = m.build(&parse("!(!a & !b)").unwrap()).unwrap();
        let report = equiv_edges(&m, a, b, &["a".into(), "b".into()], DEFAULT_VAR_CAP).unwrap();
        assert!(report.canonical_equal);
        assert!(report.truth_table_equivalent);
        assert_eq!(report.assignments_checked, 4);

        let c = m.build(&parse("a & b").unwrap()).unwrap();
        let report = equiv_edges(&m, a, c, &["a".into(), "b".into()], DEFAULT_VAR_CAP).unwrap();
        assert!(!report.canonical_equal);
        assert!(!report.truth_table_equivalent);
        assert!(report.witness.is_some());
    }
}
