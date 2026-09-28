//! Evidence verification — the independent checker layer.
//!
//! The solver produces witnesses; this module re-validates them against the
//! original constraints rather than trusting kernel bookkeeping. It exists so
//! that:
//!
//! * the HTTP layer never serves a feasible assignment that violates even
//!   one constraint, nor a "conflict cycle" that is not a closed, strictly
//!   negative walk of real constraint ids;
//! * tests have a reference checker that is structurally different from the
//!   solver (straight-line constraint evaluation, no shortest paths) to
//!   assert against.

use std::collections::{BTreeMap, BTreeSet};

use serde::Serialize;

use crate::error::{ServiceError, ServiceResult};
use crate::model::Constraint;

/// One constraint violated by a proposed assignment.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct Violation {
    pub constraint_id: String,
    pub lhs: String,
    pub rhs: String,
    pub bound: i64,
    pub lhs_value: i64,
    pub rhs_value: i64,
    pub difference: i64,
}

/// Result of checking a full assignment.
#[derive(Debug, Clone, Serialize)]
pub struct AssignmentCheck {
    pub satisfied: bool,
    pub checked_constraints: usize,
    pub violations: Vec<Violation>,
    /// Variable names in the assignment that no constraint mentions. Not a
    /// failure by itself, but reported so callers notice stale variables.
    pub extra_variables: Vec<String>,
}

/// Verify `assignment` satisfies every constraint.
///
/// * missing variable → [`ErrorKind::Input`];
/// * subtraction overflow → [`ErrorKind::ComputationFailed`].
///
/// [`ErrorKind::Input`]: crate::error::ErrorKind::Input
/// [`ErrorKind::ComputationFailed`]: crate::error::ErrorKind::ComputationFailed
pub fn verify_assignment(
    constraints: &[Constraint],
    assignment: &BTreeMap<String, i64>,
) -> ServiceResult<AssignmentCheck> {
    let mut used: BTreeSet<&str> = BTreeSet::new();
    let mut violations = Vec::new();
    for c in constraints {
        used.insert(c.lhs.as_str());
        used.insert(c.rhs.as_str());
        let x = *assignment.get(&c.lhs).ok_or_else(|| {
            ServiceError::input(format!("assignment is missing variable '{}'", c.lhs))
        })?;
        let y = *assignment.get(&c.rhs).ok_or_else(|| {
            ServiceError::input(format!("assignment is missing variable '{}'", c.rhs))
        })?;
        let diff = x.checked_sub(y).ok_or_else(|| {
            ServiceError::computation_failed(format!(
                "verifying constraint '{}': {x} - {y} overflows i64",
                c.id
            ))
        })?;
        if diff > c.bound {
            violations.push(Violation {
                constraint_id: c.id.clone(),
                lhs: c.lhs.clone(),
                rhs: c.rhs.clone(),
                bound: c.bound,
                lhs_value: x,
                rhs_value: y,
                difference: diff,
            });
        }
    }
    let mut extra_variables: Vec<String> = assignment
        .keys()
        .filter(|v| !used.contains(v.as_str()))
        .cloned()
        .collect();
    extra_variables.sort();
    Ok(AssignmentCheck {
        satisfied: violations.is_empty(),
        checked_constraints: constraints.len(),
        violations,
        extra_variables,
    })
}

/// Why an alleged conflict cycle was rejected.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(tag = "reason", rename_all = "snake_case")]
pub enum CycleRejection {
    Empty,
    UnknownConstraint { constraint_id: String },
    NotClosed { from: String, to: String },
    DuplicateEdge { constraint_id: String },
    NotStrictlyNegative { weight: i128 },
    WeightOverflow,
}

#[derive(Debug, Clone, Serialize)]
pub struct CycleCheck {
    pub valid: bool,
    /// Strictly negative weight of the closed walk, when computable in i128.
    pub weight: Option<i128>,
    pub edges: usize,
    pub rejection: Option<CycleRejection>,
}

/// Verify that `cycle_ids`, in order, walk a closed directed loop whose total
/// weight is strictly negative, with every id referencing a known constraint.
///
/// Weight accumulation uses [`i128`] (the cycle length is bounded by the edge
/// count), so it can represent sums that exceed i64; such a cycle is rejected
/// as invalid evidence for this i64 service via [`CycleRejection::WeightOverflow`]
/// only when the sum cannot even be tracked — i128 overflow is astronomically
/// unlikely with bounded inputs but handled explicitly.
pub fn verify_cycle(
    constraints: &[Constraint],
    cycle_ids: &[String],
) -> ServiceResult<CycleCheck> {
    if cycle_ids.is_empty() {
        return Ok(CycleCheck {
            valid: false,
            weight: None,
            edges: 0,
            rejection: Some(CycleRejection::Empty),
        });
    }

    let by_id: BTreeMap<&str, &Constraint> =
        constraints.iter().map(|c| (c.id.as_str(), c)).collect();

    let mut seen: BTreeSet<&str> = BTreeSet::new();
    let mut weight: i128 = 0;
    let mut first_rhs: Option<&str> = None;
    let mut prev_to: Option<&str> = None;

    for id in cycle_ids {
        if !seen.insert(id.as_str()) {
            return Ok(CycleCheck {
                valid: false,
                weight: None,
                edges: cycle_ids.len(),
                rejection: Some(CycleRejection::DuplicateEdge {
                    constraint_id: id.clone(),
                }),
            });
        }
        let c = match by_id.get(id.as_str()) {
            Some(c) => *c,
            None => {
                return Ok(CycleCheck {
                    valid: false,
                    weight: None,
                    edges: cycle_ids.len(),
                    rejection: Some(CycleRejection::UnknownConstraint {
                        constraint_id: id.clone(),
                    }),
                })
            }
        };
        // Edge for `lhs - rhs <= bound` is rhs -> lhs.
        if first_rhs.is_none() {
            first_rhs = Some(c.rhs.as_str());
        }
        if let Some(p) = prev_to {
            if p != c.rhs {
                return Ok(CycleCheck {
                    valid: false,
                    weight: None,
                    edges: cycle_ids.len(),
                    rejection: Some(CycleRejection::NotClosed {
                        from: p.to_string(),
                        to: c.rhs.clone(),
                    }),
                });
            }
        }
        weight = weight.checked_add(c.bound as i128).ok_or_else(|| {
            ServiceError::computation_failed("cycle weight accumulation overflows i128")
        })?;
        prev_to = Some(c.lhs.as_str());
    }

    let closes = prev_to == first_rhs;
    if !closes {
        return Ok(CycleCheck {
            valid: false,
            weight: Some(weight),
            edges: cycle_ids.len(),
            rejection: Some(CycleRejection::NotClosed {
                from: prev_to.unwrap_or("").to_string(),
                to: first_rhs.unwrap_or("").to_string(),
            }),
        });
    }
    if weight >= 0 {
        return Ok(CycleCheck {
            valid: false,
            weight: Some(weight),
            edges: cycle_ids.len(),
            rejection: Some(CycleRejection::NotStrictlyNegative { weight }),
        });
    }
    Ok(CycleCheck {
        valid: true,
        weight: Some(weight),
        edges: cycle_ids.len(),
        rejection: None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn c(id: &str, x: &str, y: &str, w: i64) -> Constraint {
        Constraint::new(id, x, y, w).unwrap()
    }

    #[test]
    fn assignment_violations_are_individually_reported() {
        let cs = vec![c("a", "x", "y", 1), c("b", "y", "z", -2)];
        let mut a = BTreeMap::new();
        a.insert("x".to_string(), 5);
        a.insert("y".to_string(), 0);
        a.insert("z".to_string(), 2); // 0 - 2 = -2 <= -2, so only a violated
        let check = verify_assignment(&cs, &a).unwrap();
        assert!(!check.satisfied);
        assert_eq!(check.violations.len(), 1);
        let v = &check.violations[0];
        assert_eq!(v.constraint_id, "a");
        assert_eq!(v.difference, 5);
    }

    #[test]
    fn cycle_must_be_closed_and_negative() {
        // x - y <= -1 (y->x, -1), y - z <= -1 (z->y,-1), z - x <= 1 (x->z,1)
        // sum = -1 < 0, closed: y->x->z->y
        let cs = vec![c("a", "x", "y", -1), c("b", "z", "x", 1), c("c", "y", "z", -1)];
        let good = verify_cycle(&cs, &["a".into(), "b".into(), "c".into()]).unwrap();
        assert!(good.valid);
        assert_eq!(good.weight, Some(-1));

        // rotate start, still valid
        let rot = verify_cycle(&cs, &["b".into(), "c".into(), "a".into()]).unwrap();
        assert!(rot.valid);

        // reversed order is not a directed walk
        let bad = verify_cycle(&cs, &["a".into(), "c".into(), "b".into()]).unwrap();
        assert!(!bad.valid);
        assert!(matches!(bad.rejection, Some(CycleRejection::NotClosed { .. })));

        // zero-weight cycle is not evidence
        let zcs = vec![c("a", "x", "y", 1), c("b", "y", "x", -1)];
        let zero = verify_cycle(&zcs, &["a".into(), "b".into()]).unwrap();
        assert!(!zero.valid);
        assert_eq!(
            zero.rejection,
            Some(CycleRejection::NotStrictlyNegative { weight: 0 })
        );
    }
}
