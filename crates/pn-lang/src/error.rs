//! Typed, classified input errors.

use thiserror::Error;

/// Stable failure categories asserted on by the independent test suite.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputErrorCategory {
    /// Body is not syntactically JSON.
    Syntax,
    /// JSON has the wrong shape, type or a missing required field.
    Schema,
    /// Well-formed request describing an invalid model.
    Semantic,
}

impl InputErrorCategory {
    pub fn as_str(self) -> &'static str {
        match self {
            InputErrorCategory::Syntax => "SYNTAX",
            InputErrorCategory::Schema => "SCHEMA",
            InputErrorCategory::Semantic => "SEMANTIC",
        }
    }
}

/// A single input failure with a JSON-Pointer-ish location.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("[{}] at {pointer}: {message}", category.as_str())]
pub struct InputError {
    pub category: InputErrorCategory,
    pub message: String,
    /// Location inside the request document, e.g. `/places/0/capacity`.
    pub pointer: String,
}

/// One or more classified input failures. The server maps this to HTTP 400;
/// it is never mapped to a successful verdict.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
#[error("{errors:?}")]
pub struct InputErrorReport {
    pub errors: Vec<InputError>,
}

impl InputErrorReport {
    pub fn schema(message: impl Into<String>, pointer: impl Into<String>) -> Self {
        InputErrorReport {
            errors: vec![InputError {
                category: InputErrorCategory::Schema,
                message: message.into(),
                pointer: pointer.into(),
            }],
        }
    }

    pub fn semantic(message: impl Into<String>, pointer: impl Into<String>) -> Self {
        InputErrorReport {
            errors: vec![InputError {
                category: InputErrorCategory::Semantic,
                message: message.into(),
                pointer: pointer.into(),
            }],
        }
    }

    /// Highest-severity category present, for HTTP-level summaries.
    pub fn primary_category(&self) -> InputErrorCategory {
        if self
            .errors
            .iter()
            .any(|e| e.category == InputErrorCategory::Syntax)
        {
            InputErrorCategory::Syntax
        } else if self
            .errors
            .iter()
            .any(|e| e.category == InputErrorCategory::Schema)
        {
            InputErrorCategory::Schema
        } else {
            InputErrorCategory::Semantic
        }
    }
}
