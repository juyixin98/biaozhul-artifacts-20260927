//! AST of the Interval-analysis Language (IAL).
//!
//! Grammar (see docs/SEMANTICS.md):
//!
//! ```text
//! program     := decl* block
//! decl        := "input" IDENT "[" INT ":" INT "]" ";"
//!              | "const" IDENT "=" INT ";"
//!              | "array" IDENT "[" INT "]" ";"
//! block       := "{" stmt* "}"
//! stmt        := block
//!              | lvalue "=" expr ";"
//!              | "if" "(" expr ")" stmt ("else" stmt)?
//!              | "while" "(" expr ")" stmt
//!              | "assert" "(" expr ")" ";"
//!              | "skip" ";"
//! lvalue      := IDENT ("[" expr "]")?
//! expr        := precedence-climbed; see parser.
//! ```
//!
//! Integers are *bounded* signed 64-bit values at every level: there is no
//! un-bounded mathematical integer type in the language.
use crate::span::Span;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Program {
    pub decls: Vec<Decl>,
    pub body: Block,
    pub span: Span,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Block {
    pub stmts: Vec<Stmt>,
    pub span: Span,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Decl {
    /// `input x [lo:hi];` — non-deterministic value drawn from the closed
    /// interval `[lo, hi]`. This is what concrete execution enumerates over.
    Input {
        name: String,
        lo: i64,
        hi: i64,
        span: Span,
        name_span: Span,
    },
    /// `const c = k;` — compile-time integer constant, inlined into the
    /// initial environment.
    Const {
        name: String,
        value: i64,
        span: Span,
        name_span: Span,
    },
    /// `array a[n];` — zero-initialised, length `n` (`1 <= n`).
    Array {
        name: String,
        len: u64,
        span: Span,
        name_span: Span,
    },
}

impl Decl {
    pub fn name(&self) -> &str {
        match self {
            Decl::Input { name, .. } | Decl::Const { name, .. } | Decl::Array { name, .. } => name,
        }
    }
    pub fn span(&self) -> Span {
        match self {
            Decl::Input { span, .. } | Decl::Const { span, .. } | Decl::Array { span, .. } => *span,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Stmt {
    Block(Block),
    Assign {
        target: Lvalue,
        value: Expr,
        span: Span,
    },
    If {
        cond: Expr,
        then: Box<Stmt>,
        otherwise: Option<Box<Stmt>>,
        span: Span,
    },
    While {
        cond: Expr,
        body: Box<Stmt>,
        span: Span,
    },
    /// `assert(e);` — condition the abstract state; concrete execution fails
    /// the run if `e == 0`. Assertions are verification probes, not errors to
    /// report by themselves.
    Assert { cond: Expr, span: Span },
    Skip {
        span: Span,
    },
}

impl Stmt {
    pub fn span(&self) -> Span {
        match self {
            Stmt::Block(b) => b.span,
            Stmt::Assign { span, .. }
            | Stmt::If { span, .. }
            | Stmt::While { span, .. }
            | Stmt::Assert { span, .. }
            | Stmt::Skip { span } => *span,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lvalue {
    pub name: String,
    /// `None` for a scalar; `Some` for `arr[index]`.
    pub index: Option<Expr>,
    pub span: Span,
    pub name_span: Span,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Expr {
    pub kind: ExprKind,
    pub span: Span,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum ExprKind {
    Int(i64),
    Var(String),
    ArrayRead { name: String, index: Box<Expr> },
    Unary { op: UnOp, inner: Box<Expr> },
    Binary { op: BinOp, lhs: Box<Expr>, rhs: Box<Expr> },
    // Ternary condition embedded in an expression is intentionally absent to
    // keep refinement local to statements.
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum UnOp {
    Neg,
    /// Logical not: `!0 == 1`, `!x == 0` for `x != 0`.
    Not,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum BinOp {
    // Bounded signed arithmetic (overflow is a runtime failure).
    Add,
    Sub,
    Mul,
    Div,
    Mod,
    // Comparisons yield exactly 0 or 1.
    Lt,
    Le,
    Gt,
    Ge,
    Eq,
    Ne,
    // Logical operators treat 0 as false, any non-zero as true; they never
    // short-circuit (no order of evaluation to reason about) and never fail.
    And,
    Or,
}

impl BinOp {
    pub fn is_arithmetic(self) -> bool {
        matches!(
            self,
            BinOp::Add | BinOp::Sub | BinOp::Mul | BinOp::Div | BinOp::Mod
        )
    }
    pub fn is_comparison(self) -> bool {
        matches!(
            self,
            BinOp::Lt | BinOp::Le | BinOp::Gt | BinOp::Ge | BinOp::Eq | BinOp::Ne
        )
    }
}
