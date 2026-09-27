//! The Boolean input language: its abstract syntax.
//!
//! The AST is deliberately tiny and has no dependency on the BDD kernel, so
//! the independent truth-table oracle in [`crate::oracle`] and the parser
//! tests can consume the same syntax trees without touching the solver.

use crate::kernel::Op;

/// A Boolean expression.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Expr {
    /// `true` / `false`
    Const(bool),
    /// A named variable (resolved against a manager's order later).
    Var(String),
    /// Logical negation.
    Not(Box<Expr>),
    /// A binary connective application.
    Binary {
        op: BinOp,
        lhs: Box<Expr>,
        rhs: Box<Expr>,
    },
}

impl Expr {
    /// All variable names occurring in the expression, first-occurrence
    /// order, deduplicated.
    pub fn variables(&self) -> Vec<String> {
        let mut seen = std::collections::BTreeSet::new();
        let mut out = Vec::new();
        self.collect_vars(&mut seen, &mut out);
        out
    }

    fn collect_vars(&self, seen: &mut std::collections::BTreeSet<String>, out: &mut Vec<String>) {
        match self {
            Expr::Const(_) => {}
            Expr::Var(name) => {
                if seen.insert(name.clone()) {
                    out.push(name.clone());
                }
            }
            Expr::Not(e) => e.collect_vars(seen, out),
            Expr::Binary { lhs, rhs, .. } => {
                lhs.collect_vars(seen, out);
                rhs.collect_vars(seen, out);
            }
        }
    }
}

/// Binary connectives of the surface language, one-to-one with kernel [`Op`]s.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BinOp {
    And,
    Or,
    Xor,
    Implies,
    Equiv,
}

impl BinOp {
    /// The kernel operation this surface connective maps to.
    pub fn kernel_op(self) -> Op {
        match self {
            BinOp::And => Op::And,
            BinOp::Or => Op::Or,
            BinOp::Xor => Op::Xor,
            BinOp::Implies => Op::Implies,
            BinOp::Equiv => Op::Equiv,
        }
    }

    /// Spelling used in error messages and serialization.
    pub fn as_str(self) -> &'static str {
        match self {
            BinOp::And => "&&",
            BinOp::Or => "||",
            BinOp::Xor => "^",
            BinOp::Implies => "->",
            BinOp::Equiv => "<->",
        }
    }
}
