use std::fmt;

use serde::Serialize;

pub use crate::ast::BuildErrorKind;

/// Error raised while building a [`crate::System`] from either input format.
#[derive(Debug, Clone, Serialize)]
pub struct BuildError {
    pub kind: BuildErrorKind,
    pub message: String,
    /// 1-based (line, column) for textual input; absent for JSON input.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub position: Option<(usize, usize)>,
}

impl BuildError {
    pub fn new(kind: BuildErrorKind, message: impl Into<String>) -> Self {
        BuildError {
            kind,
            message: message.into(),
            position: None,
        }
    }

    pub fn with_source_pos(mut self, _byte: usize, lc: (usize, usize)) -> Self {
        self.position = Some(lc);
        self
    }
}

impl fmt::Display for BuildError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:?}: {}", self.kind, self.message)?;
        if let Some((line, col)) = self.position {
            write!(f, " (line {line}, column {col})")?;
        }
        Ok(())
    }
}

impl std::error::Error for BuildError {}
