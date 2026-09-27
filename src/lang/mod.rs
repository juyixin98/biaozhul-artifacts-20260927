//! Input language for Boolean functions.
//!
//! * [`ast`] — syntax tree and connectives
//! * [`lexer`] — position-annotated tokenizer
//! * [`parser`] — recursive-descent parser
//!
//! The entry point [`parse`] turns source text into an [`Expr`].

pub(crate) mod ast;
pub(crate) mod lexer;
pub(crate) mod parser;

pub use ast::{BinOp, Expr};
pub use lexer::{LexError, Span};
pub use parser::{parse, ParseError};

#[cfg(test)]
mod lang_tests;
