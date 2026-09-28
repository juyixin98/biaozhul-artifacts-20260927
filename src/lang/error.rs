use super::ast::Span;
use std::fmt;

/// Error produced by the lexer or the parser; always carries a source position.
#[derive(Debug, Clone, PartialEq)]
pub struct LangError {
    pub message: String,
    pub span: Span,
}

impl LangError {
    pub fn new(message: impl Into<String>, span: Span) -> Self {
        LangError {
            message: message.into(),
            span,
        }
    }
}

impl fmt::Display for LangError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at {}", self.message, self.span)
    }
}

impl std::error::Error for LangError {}
