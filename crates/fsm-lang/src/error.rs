//! Errors shared across language, kernel and evidence layers.

use serde::{Deserialize, Serialize};

/// A static (compile-time) specification error.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LangError {
    pub code: String,
    pub message: String,
}

impl LangError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }
}

impl std::fmt::Display for LangError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

/// Error raised while evaluating an expression over a concrete state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvalError {
    pub code: String,
    pub message: String,
}

impl EvalError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }
}

/// Error raised while exploring a transition target state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StateError {
    pub code: String,
    pub message: String,
    pub transition: String,
}

impl StateError {
    pub fn new(code: &str, transition: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
            transition: transition.to_string(),
        }
    }
}
