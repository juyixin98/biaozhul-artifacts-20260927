//! Input-language crate: lexer, AST, parser and static validation for IAL.

pub mod ast;
pub mod lexer;
pub mod parser;
pub mod span;
pub mod validate;

pub use ast::*;
pub use parser::{parse, render_source_line, ParseError};
pub use span::{loc_at, Loc, Span};
pub use validate::{resolve, ArrayInfo, DeclKind, InputInfo, ProgramInfo, ValidationError};

/// Crate semantic version, surfaced in analysis reports for traceability.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
