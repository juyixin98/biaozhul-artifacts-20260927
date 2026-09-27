//! Transition semantics and state rendering.
//!
//! The key rule is the one the whole project is built around: a transition is
//! a **parallel** assignment. Every RHS is evaluated against the same
//! pre-state; only afterwards are targets committed. There is no ordering
//! within an assignment list, so `x := y, y := x` swaps.

use serde_json::{Map, Value as Json};

use crate::eval::{eval, EvalError};
use crate::system::{System, Value};

impl System {
    /// Resolve an enum variant name to its integer value.
    pub fn variant_value(&self, name: &str) -> Option<i64> {
        for v in &self.vars {
            if let crate::Domain::Enum { variants } = &v.domain {
                if let Some(i) = variants.iter().position(|x| x == name) {
                    return Some(i as i64);
                }
            }
        }
        None
    }

    /// True when the state satisfies the legal-termination predicate.
    pub fn is_terminal(&self, st: &[Value]) -> Result<bool, EvalError> {
        eval(self, &self.terminal.clone(), st)?
            .as_bool()
            .map_err(|_| {
                EvalError::new(
                    crate::EvalErrorKind::TypeMismatch,
                    "terminal predicate did not return a boolean",
                )
            })
    }

    /// Evaluate a transition's guard.
    pub fn guard_holds(&self, t: &crate::Transition, st: &[Value]) -> Result<bool, EvalError> {
        eval(self, &t.guard, st)?.as_bool().map_err(|_| {
            EvalError::new(
                crate::EvalErrorKind::TypeMismatch,
                format!("guard of '{}' is not boolean", t.name),
            )
        })
    }

    /// Apply a transition. All RHS expressions are evaluated against `pre`
    /// first; only then are target slots filled and domain-checked.
    pub fn apply(&self, t: &crate::Transition, pre: &[Value]) -> Result<Vec<Value>, EvalError> {
        let mut computed: Vec<(usize, Value)> = Vec::with_capacity(t.assign.len());
        for a in &t.assign {
            let v = eval(self, &a.rhs, pre)?;
            let idx = a.target_index.expect("built systems resolve targets");
            computed.push((idx, v));
        }
        let mut post = pre.to_vec();
        for (idx, v) in computed {
            if !self.vars[idx].domain.contains(&v) {
                return Err(EvalError::new(
                    crate::EvalErrorKind::OutOfDomain,
                    format!(
                        "transition '{}' assigns {v} to '{}', outside its domain",
                        t.name, self.vars[idx].name
                    ),
                ));
            }
            post[idx] = v;
        }
        Ok(post)
    }

    /// Render a state as an ordered JSON object `{var: value}`, with enum
    /// integers rendered as their variant name.
    pub fn render_state(&self, st: &[Value]) -> Map<String, Json> {
        let mut m = Map::new();
        for (v, val) in self.vars.iter().zip(st) {
            m.insert(v.name.clone(), render_value(&v.domain, *val));
        }
        m
    }
}

/// Render one value: enums emit their variant *name*, booleans JSON booleans,
/// ints JSON numbers.
pub fn render_value(domain: &crate::Domain, v: Value) -> Json {
    match (domain, v) {
        (_, Value::Bool(b)) => Json::Bool(b),
        (crate::Domain::Enum { variants }, Value::Int(i)) => {
            if (0..variants.len() as i64).contains(&i) {
                Json::String(variants[i as usize].clone())
            } else {
                Json::from(i)
            }
        }
        (_, Value::Int(i)) => Json::from(i),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::eval::build_system;
    use crate::parser::parse_system;

    fn sys(src: &str) -> System {
        build_system(parse_system(src).unwrap()).unwrap()
    }

    #[test]
    fn parallel_assignment_swap_uses_one_prestate() {
        let s = sys(
            "system sw { var { x: int[0..3]; y: int[0..3] } init { x := 1, y := 2 } \
             transition t { guard: true; then: x := y, y := x } }",
        );
        let pre = vec![Value::Int(1), Value::Int(2)];
        let post = s.apply(&s.transitions[0], &pre).unwrap();
        // sequential assignment would give (2,2); parallel gives (2,1)
        assert_eq!(post, vec![Value::Int(2), Value::Int(1)]);
    }

    #[test]
    fn unassigned_variables_keep_prestate_value() {
        let s = sys(
            "system k { var { a: bool; b: bool } init { a := false, b := true } \
             transition t { guard: true; then: a := true } }",
        );
        let pre = vec![Value::Bool(false), Value::Bool(true)];
        let post = s.apply(&s.transitions[0], &pre).unwrap();
        assert_eq!(post, vec![Value::Bool(true), Value::Bool(true)]);
    }

    #[test]
    fn division_by_zero_is_an_eval_error_not_a_panic() {
        let s = sys("system z { var { x: int[0..1] } init { x := 0 } transition t { guard: x == 0; then: x := x } terminal { x / 0 == 1 } }");
        let st = vec![Value::Int(0)];
        assert!(s.is_terminal(&st).is_err());
    }

    #[test]
    fn out_of_domain_update_is_rejected() {
        // guard can only be true in-domain but an arithmetic RHS may leave it;
        // construct RHS that overflows the declared range.
        let s = sys("system o { var { x: int[0..1] } init { x := 0 } \
             transition t { guard: x == 0; then: x := x + 2 } }");
        let st = vec![Value::Int(0)];
        let e = s.apply(&s.transitions[0], &st).unwrap_err();
        assert_eq!(e.kind, crate::EvalErrorKind::OutOfDomain);
    }
}
