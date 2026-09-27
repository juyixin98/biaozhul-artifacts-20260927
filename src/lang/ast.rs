//! Abstract syntax tree.
//!
//! Nodes carry a stable `id` (assigned deterministically after parsing or JSON
//! loading by [`ast::number_program`]) and a source span. The concrete
//! interpreter and the symbolic engine refer to the same ids, which lets a
//! counterexample replay prove it reaches the *same* failing node.

use serde::{Deserialize, Serialize};

use crate::lang::types::Type;

#[derive(Debug, Clone)]
pub struct Program {
    pub params: Vec<Param>,
    pub body: Vec<Stmt>,
}

#[derive(Debug, Clone)]
pub struct Param {
    pub name: String,
    pub ty: Type,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Span {
    pub line: u32,
    pub col_start: u32,
    pub col_end: u32,
}

#[derive(Debug, Clone)]
pub struct Stmt {
    pub id: NodeId,
    pub kind: StmtKind,
    pub span: Span,
}

#[derive(Debug, Clone)]
pub enum StmtKind {
    /// `let x: uN = expr;`
    Let {
        name: String,
        ty: Type,
        value: Expr,
    },
    /// `x = expr;`
    Assign { name: String, value: Expr },
    /// `assert(expr, "message");` — message optional
    Assert { cond: Expr, message: Option<String> },
    /// `assume(expr);` — prunes infeasible branches
    Assume { cond: Expr },
    If {
        cond: Expr,
        then: Vec<Stmt>,
        els: Vec<Stmt>,
    },
    While {
        cond: Expr,
        body: Vec<Stmt>,
    },
}

#[derive(Debug, Clone)]
pub struct Expr {
    pub id: NodeId,
    pub kind: ExprKind,
    /// Filled by the type checker; arithmetic nodes carry their bit width,
    /// relational / logical nodes stay `None`.
    pub ty: Option<Type>,
    pub span: Span,
}

impl Expr {
    pub fn new(kind: ExprKind, span: Span) -> Self {
        Expr {
            id: NodeId::placeholder(),
            kind,
            ty: None,
            span,
        }
    }
}

#[derive(Debug, Clone)]
pub enum ExprKind {
    Lit(Lit),
    /// `true` / `false`
    BoolLit(bool),
    Var(String),
    Un(UnOp, Box<Expr>),
    Bin(BinOp, Box<Expr>, Box<Expr>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Lit {
    pub value: u64,
    /// Width annotation when written with suffix, e.g. `1u8`; `100` is None.
    pub suffix: Option<Type>,
    pub radix: Radix,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Radix {
    Dec,
    Hex,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum UnOp {
    /// Unary minus, wrapping (`-x == 2^N - x`).
    Neg,
    /// Bitwise complement.
    BitNot,
    /// Logical negation (boolean operand).
    Not,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum BinOp {
    // wrapping integer arithmetic
    Add,
    Sub,
    Mul,
    /// Unsigned division (guard: divisor != 0).
    Div,
    /// Unsigned remainder (guard: divisor != 0).
    Rem,
    // bitwise
    BitAnd,
    BitOr,
    BitXor,
    // shifts (shift amount reduced mod width)
    Shl,
    Shr,
    // unsigned comparisons -> bool
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    // logical -> bool
    LAnd,
    LOr,
}

impl BinOp {
    /// Whether the result of this operator is a boolean rather than a bitvector.
    pub fn is_boolean(self) -> bool {
        matches!(
            self,
            BinOp::Eq
                | BinOp::Ne
                | BinOp::Lt
                | BinOp::Le
                | BinOp::Gt
                | BinOp::Ge
                | BinOp::LAnd
                | BinOp::LOr
        )
    }

    /// Whether evaluation requires a zero-divisor guard.
    pub fn guards_divisor(self) -> bool {
        matches!(self, BinOp::Div | BinOp::Rem)
    }

    pub fn symbol(self) -> &'static str {
        match self {
            BinOp::Add => "+",
            BinOp::Sub => "-",
            BinOp::Mul => "*",
            BinOp::Div => "/",
            BinOp::Rem => "%",
            BinOp::BitAnd => "&",
            BinOp::BitOr => "|",
            BinOp::BitXor => "^",
            BinOp::Shl => "<<",
            BinOp::Shr => ">>",
            BinOp::Eq => "==",
            BinOp::Ne => "!=",
            BinOp::Lt => "<",
            BinOp::Le => "<=",
            BinOp::Gt => ">",
            BinOp::Ge => ">=",
            BinOp::LAnd => "&&",
            BinOp::LOr => "||",
        }
    }
}

/// Deterministic node identifier, stable for the same program text.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct NodeId(pub u32);

impl NodeId {
    pub fn placeholder() -> Self {
        NodeId(u32::MAX)
    }
    pub fn get(self) -> u32 {
        self.0
    }
}

/// Visitor mutating ids in deterministic DFS pre-order.
struct Numberer {
    next: u32,
}

impl Numberer {
    fn stmt(&mut self, s: &mut Stmt) {
        s.id = NodeId(self.next);
        self.next += 1;
        match &mut s.kind {
            StmtKind::Let { value, .. } => self.expr(value),
            StmtKind::Assign { value, .. } => self.expr(value),
            StmtKind::Assert { cond, .. } => self.expr(cond),
            StmtKind::Assume { cond } => self.expr(cond),
            StmtKind::If { cond, then, els } => {
                self.expr(cond);
                for t in then.iter_mut() {
                    self.stmt(t);
                }
                for e in els.iter_mut() {
                    self.stmt(e);
                }
            }
            StmtKind::While { cond, body } => {
                self.expr(cond);
                for b in body.iter_mut() {
                    self.stmt(b);
                }
            }
        }
    }

    fn expr(&mut self, e: &mut Expr) {
        e.id = NodeId(self.next);
        self.next += 1;
        match &mut e.kind {
            ExprKind::Lit(_) | ExprKind::BoolLit(_) | ExprKind::Var(_) => {}
            ExprKind::Un(_, inner) => self.expr(inner),
            ExprKind::Bin(_, a, b) => {
                self.expr(a);
                self.expr(b);
            }
        }
    }
}

/// Assign deterministic ids to every node. Run once after parsing or JSON load.
pub fn number_program(p: &mut Program) {
    let mut n = Numberer { next: 0 };
    for s in p.body.iter_mut() {
        n.stmt(s);
    }
}
