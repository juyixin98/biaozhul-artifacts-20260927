//! Input language: AST, types, lexer, parser, validator.
//!
//! The language is a small imperative language over fixed-width *unsigned*
//! integers with structured control flow (if / while) and no unbounded
//! constructs. All programs are total under a loop-unrolling bound supplied
//! by the analysis configuration.

pub mod ast;
pub mod check;
pub mod lex;
pub mod parse;
pub mod types;

pub use ast::{BinOp, Expr, ExprKind, Lit, Program, Stmt, StmtKind, UnOp};
pub use check::{check_program, CheckError};
pub use parse::{parse_program, ParseError};
pub use types::{mask_of, Type};

use serde::Deserialize;

/// Error returned when loading a program from source text.
#[derive(Debug, thiserror::Error)]
pub enum LangError {
    #[error("parse error: {0}")]
    Parse(#[from] ParseError),
    #[error("check error: {0}")]
    Check(#[from] CheckError),
}

/// Parse and validate a program from its textual source.
pub fn program_from_source(src: &str) -> Result<Program, LangError> {
    let mut p = parse_program(src)?;
    check_program(&mut p)?;
    Ok(p)
}

/// JSON envelope accepted by the HTTP API as an alternative to source text.
#[derive(Debug, Deserialize)]
pub struct ProgramJson {
    /// Program parameters as `"name: uN"` strings, in declaration order.
    pub params: Vec<String>,
    /// Statement lines (the DSL body without the outer braces).
    pub body: Vec<String>,
}

/// Build a program from a JSON AST-lite envelope.
pub fn program_from_json(j: &ProgramJson) -> Result<Program, LangError> {
    let mut src = String::new();
    for p in &j.params {
        src.push_str("param ");
        src.push_str(p);
        src.push_str(";\n");
    }
    for line in &j.body {
        src.push_str(line);
        src.push('\n');
    }
    program_from_source(&src)
}
