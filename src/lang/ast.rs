use serde::{Deserialize, Serialize};
use std::fmt;

/// Source position, 1-based line/column, byte offset and length.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Span {
    pub line: u32,
    pub col: u32,
    pub offset: u32,
    pub len: u32,
}

impl Span {
    pub fn new(line: u32, col: u32, offset: u32, len: u32) -> Self {
        Span {
            line,
            col,
            offset,
            len,
        }
    }
}

impl fmt::Display for Span {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "L{}:C{}", self.line, self.col)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Stmt {
    /// `let x: [lo, hi];` — declares an input variable ranging over [lo, hi].
    Input {
        name: String,
        lo: i64,
        hi: i64,
        span: Span,
    },
    /// `array a[N];` — declares a fixed-length array, elements initialised to 0.
    ArrayDecl {
        name: String,
        len: usize,
        span: Span,
    },
    /// `x := e;`
    Assign {
        name: String,
        expr: Expr,
        span: Span,
    },
    /// `a[i] := e;`
    ArrayStore {
        name: String,
        index: Expr,
        value: Expr,
        span: Span,
    },
    If {
        cond: Cond,
        then_body: Vec<Stmt>,
        else_body: Vec<Stmt>,
        span: Span,
    },
    While {
        cond: Cond,
        body: Vec<Stmt>,
        span: Span,
    },
    Assert {
        cond: Cond,
        span: Span,
    },
}

impl Stmt {
    pub fn span(&self) -> Span {
        match self {
            Stmt::Input { span, .. }
            | Stmt::ArrayDecl { span, .. }
            | Stmt::Assign { span, .. }
            | Stmt::ArrayStore { span, .. }
            | Stmt::If { span, .. }
            | Stmt::While { span, .. }
            | Stmt::Assert { span, .. } => *span,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Expr {
    Int(i64, Span),
    Var(String, Span),
    Load {
        array: String,
        index: Box<Expr>,
        span: Span,
    },
    Neg(Box<Expr>, Span),
    Add(Box<Expr>, Box<Expr>, Span),
    Sub(Box<Expr>, Box<Expr>, Span),
    Mul(Box<Expr>, Box<Expr>, Span),
}

impl Expr {
    pub fn span(&self) -> Span {
        match self {
            Expr::Int(_, s)
            | Expr::Var(_, s)
            | Expr::Load { span: s, .. }
            | Expr::Neg(_, s)
            | Expr::Add(_, _, s)
            | Expr::Sub(_, _, s)
            | Expr::Mul(_, _, s) => *s,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum CmpOp {
    Lt,
    Le,
    Eq,
    Ne,
    Ge,
    Gt,
}

impl CmpOp {
    pub fn negate(self) -> CmpOp {
        match self {
            CmpOp::Lt => CmpOp::Ge,
            CmpOp::Le => CmpOp::Gt,
            CmpOp::Eq => CmpOp::Ne,
            CmpOp::Ne => CmpOp::Eq,
            CmpOp::Ge => CmpOp::Lt,
            CmpOp::Gt => CmpOp::Le,
        }
    }

    pub fn flip(self) -> CmpOp {
        match self {
            CmpOp::Lt => CmpOp::Gt,
            CmpOp::Le => CmpOp::Ge,
            CmpOp::Eq => CmpOp::Eq,
            CmpOp::Ne => CmpOp::Ne,
            CmpOp::Ge => CmpOp::Le,
            CmpOp::Gt => CmpOp::Lt,
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Cond {
    Cmp {
        op: CmpOp,
        lhs: Expr,
        rhs: Expr,
        span: Span,
    },
    And(Box<Cond>, Box<Cond>, Span),
    Or(Box<Cond>, Box<Cond>, Span),
    Not(Box<Cond>, Span),
}

impl Cond {
    pub fn span(&self) -> Span {
        match self {
            Cond::Cmp { span, .. }
            | Cond::And(_, _, span)
            | Cond::Or(_, _, span)
            | Cond::Not(_, span) => *span,
        }
    }
}
