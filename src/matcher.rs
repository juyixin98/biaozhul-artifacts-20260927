//! Predicate and event-pattern evaluation.
//!
//! Kept in its own module so both the online kernel and the independently
//! written offline oracle can evaluate the *language* through one shared,
//! well-tested primitive, while their obligation bookkeeping stays separate.

use serde_json::Value;

use crate::error::{AppError, AppResult};
use crate::language::{step_view, EventPattern, Predicate, Step};

/// Does the event pattern match this step?
///
/// Matching requires equal event types and, when present, a satisfied
/// `where` predicate.
pub fn pattern_matches(step: &Step, pattern: &EventPattern) -> AppResult<bool> {
    if step.event.event_type != pattern.event_type {
        return Ok(false);
    }
    match &pattern.where_ {
        None => Ok(true),
        Some(p) => eval_predicate(step, p),
    }
}

/// Evaluate a predicate against a step.
pub fn eval_predicate(step: &Step, pred: &Predicate) -> AppResult<bool> {
    let view = step_view(step);
    eval(&view, pred)
}

fn eval(view: &crate::language::StepView<'_>, pred: &Predicate) -> AppResult<bool> {
    match pred {
        Predicate::Eq { path, value } => Ok(view.lookup(path).as_ref() == Some(value)),
        Predicate::Ne { path, value } => {
            // A missing fact is "not equal": absence is distinct from value.
            Ok(view.lookup(path).as_ref() != Some(value))
        }
        Predicate::Gt { path, value } => num_compare(view, path, value, |a, b| a > b),
        Predicate::Lt { path, value } => num_compare(view, path, value, |a, b| a < b),
        Predicate::Gte { path, value } => num_compare(view, path, value, |a, b| a >= b),
        Predicate::Lte { path, value } => num_compare(view, path, value, |a, b| a <= b),
        Predicate::In { path, values } => {
            Ok(view.lookup(path).map(|v| values.contains(&v)).unwrap_or(false))
        }
        Predicate::Exists { path } => Ok(view.lookup(path).is_some()),
        Predicate::Bool { path } => match view.lookup(path) {
            Some(Value::Bool(b)) => Ok(b),
            Some(other) => Err(AppError::compute(
                "non_boolean_fact",
                format!("fact `{path}` is {other}, expected a boolean"),
            )),
            None => Ok(false),
        },
        Predicate::Not { inner } => Ok(!eval(view, inner)?),
        Predicate::And { all } => {
            for p in all {
                if !eval(view, p)? {
                    return Ok(false);
                }
            }
            Ok(true)
        }
        Predicate::Or { any } => {
            // Full evaluation is used (short-circuit only on `true`): an
            // ill-typed operand must surface as an error even behind `or`.
            let mut result = false;
            let mut error = None;
            for p in any {
                match eval(view, p) {
                    Ok(true) => return Ok(true),
                    Ok(false) => result = false,
                    Err(e) => error = Some(e),
                }
            }
            match error {
                Some(e) if !result => Err(e),
                _ => Ok(false),
            }
        }
    }
}

fn num_compare(
    view: &crate::language::StepView<'_>,
    path: &str,
    value: &Value,
    cmp: impl FnOnce(f64, f64) -> bool,
) -> AppResult<bool> {
    let Some(actual) = view.lookup(path) else { return Ok(false) };
    let a = as_f64(&actual).ok_or_else(|| {
        AppError::compute("non_numeric_fact", format!("fact `{path}` is {actual}, expected a number"))
    })?;
    let b = as_f64(value).ok_or_else(|| {
        AppError::input("non_numeric_constant", format!("comparison constant for `{path}` is {value}"))
    })?;
    Ok(cmp(a, b))
}

fn as_f64(v: &Value) -> Option<f64> {
    v.as_f64().or_else(|| {
        // Accept integer-valued JSON numbers represented as u64/i64 too
        // (as_f64 already covers them, but be explicit about rejecting NaN).
        let n = v.as_number()?.as_f64()?;
        n.is_finite().then_some(n)
    })
}
