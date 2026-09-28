//! Domain model: a named integer difference constraint `x - y <= c`.
//!
//! [`Constraint`] is the canonical in-memory representation used by the store,
//! the graph builder and the verifier. The transport-language representations
//! (JSON DTOs and the small text DSL) live in [`crate::lang`] and
//! [`crate::http_api::dto`] and convert into this type, so the solver kernel
//! never sees strings.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::error::{ServiceError, ServiceResult};

/// Maximum length of a constraint id or variable name. Generous but bounded,
/// so a single JSON body cannot make the store unbounded in name width.
pub const MAX_NAME_LEN: usize = 128;

/// A constraint id / variable name validation rule shared by JSON and text
/// inputs:
/// * non-empty, at most [`MAX_NAME_LEN`] bytes;
/// * first character is an ASCII letter or `_`;
/// * remaining characters are ASCII letters, digits or `_`.
///
/// Hyphens are deliberately excluded: in the text DSL `x-y` must lex as
/// identifier, minus, identifier even without surrounding spaces.
pub fn validate_identifier(what: &str, raw: &str) -> ServiceResult<()> {
    if raw.is_empty() {
        return Err(ServiceError::input(format!("{what} must not be empty")));
    }
    if raw.len() > MAX_NAME_LEN {
        return Err(ServiceError::input(format!(
            "{what} is {} bytes, exceeds maximum of {MAX_NAME_LEN}",
            raw.len()
        )));
    }
    let mut chars = raw.chars();
    let first = chars.next().unwrap();
    let ok_first = first.is_ascii_alphabetic() || first == '_';
    let ok_rest = raw
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '_');
    if !ok_first || !ok_rest {
        return Err(ServiceError::input(format!(
            "{what} '{raw}' is invalid: start with a letter or underscore, then letters, digits or '_'"
        )));
    }
    Ok(())
}

/// Canonical constraint `lhs - rhs <= bound`, all values `i64`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Constraint {
    /// Client-supplied stable identifier; also the name used on the wire.
    pub id: String,
    /// Variable on the left of the subtraction.
    pub lhs: String,
    /// Variable on the right of the subtraction.
    pub rhs: String,
    /// Constant right-hand side.
    pub bound: i64,
}

impl Constraint {
    /// Construct with full validation. This is the only constructor, which
    /// guarantees the store can never hold an unvalidated constraint.
    pub fn new(id: impl Into<String>, lhs: &str, rhs: &str, bound: i64) -> ServiceResult<Self> {
        let id = id.into();
        validate_identifier("constraint id", &id)?;
        validate_identifier("variable name", lhs)?;
        validate_identifier("variable name", rhs)?;
        Ok(Self {
            id,
            lhs: lhs.to_string(),
            rhs: rhs.to_string(),
            bound,
        })
    }

    /// Re-check the inequality against a concrete assignment.
    ///
    /// Returns `Ok(true)` when satisfied, `Ok(false)` when violated, and an
    /// error when the assignment is missing a variable or evaluating the
    /// subtraction would overflow. Verification uses checked arithmetic: the
    /// kernel rejects overflow, and an assignment whose evaluation overflows
    /// cannot be called a witness either.
    pub fn check(&self, assignment: &BTreeMap<String, i64>) -> ServiceResult<bool> {
        let x = *assignment.get(&self.lhs).ok_or_else(|| {
            ServiceError::input(format!("assignment is missing variable '{}'", self.lhs))
        })?;
        let y = *assignment.get(&self.rhs).ok_or_else(|| {
            ServiceError::input(format!("assignment is missing variable '{}'", self.rhs))
        })?;
        let diff = x.checked_sub(y).ok_or_else(|| {
            ServiceError::computation_failed(format!(
                "evaluating constraint '{}': {x} - {y} overflows i64",
                self.id
            ))
        })?;
        Ok(diff <= self.bound)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifier_rules() {
        assert!(Constraint::new("c1", "x", "y", 0).is_ok());
        assert!(Constraint::new("_c", "_a", "b2", -3).is_ok());
        assert!(Constraint::new("a-b", "x", "y", 0).is_err());
        assert!(Constraint::new("", "x", "y", 0).is_err());
        assert!(Constraint::new("1c", "x", "y", 0).is_err());
        assert!(Constraint::new("c!", "x", "y", 0).is_err());
        assert!(Constraint::new(
            "c",
            "x".repeat(MAX_NAME_LEN + 1).as_str(),
            "y",
            0
        )
        .is_err());
    }

    #[test]
    fn check_uses_checked_arithmetic() {
        let c = Constraint::new("c", "x", "y", 0).unwrap();
        let mut a = BTreeMap::new();
        a.insert("x".to_string(), i64::MIN);
        a.insert("y".to_string(), 1);
        let err = c.check(&a).unwrap_err();
        assert_eq!(err.kind, crate::error::ErrorKind::ComputationFailed);
    }
}
