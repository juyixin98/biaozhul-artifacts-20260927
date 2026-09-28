//! Symbolic term language emitted by the symbolic engine.
//!
//! Terms are a small, total AST over two sorts: fixed-width bitvectors (`Bv`) and
//! booleans (`Bool`).  It intentionally mirrors SMT-LIB 2 bitvector theory closely so
//! lowering is straightforward and auditable.
//!
//! `Term` is reference-counted (`Rc`) because the same subterm typically appears both
//! as a value and inside path constraints (e.g. once in an assignment, once in a
//! branch condition).

use std::rc::Rc;

pub type Term = Rc<TermNode>;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TermNode {
    // --- bitvectors ---
    /// Literal bit pattern, always masked to the program width.
    BvConst(u64),
    /// Free input bitvector. `name` is validated to be SMT-safe during lowering.
    BvVar(String),
    BvUn(BvUnOp, Term),
    BvBin(BvBinOp, Term, Term),
    /// Boolean-to-bitvector: 1 when `t` holds else 0.
    BvIte(Term, Term, Term),

    // --- booleans ---
    BoolConst(bool),
    BoolUn(BoolUnOp, Term),
    BoolBin(BoolBinOp, Term, Term),
    /// Signedness-aware comparison with one bitwidth.
    Rel(CmpRel, Term, Term),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BvUnOp {
    Neg,
    Not,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BvBinOp {
    Add,
    Sub,
    Mul,
    Udiv,
    Urem,
    Sdiv,
    Srem,
    And,
    Or,
    Xor,
    Shl,
    LShr,
    AShr,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BoolUnOp {
    Not,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BoolBinOp {
    And,
    Or,
    Xor,
    Implies,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CmpRel {
    Eq,
    Ne,
    Ult,
    Ule,
    Ugt,
    Uge,
    Slt,
    Sle,
    Sgt,
    Sge,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Sort {
    Bv,
    Bool,
}

impl TermNode {
    pub fn sort(&self) -> Sort {
        match self {
            TermNode::BvConst(_)
            | TermNode::BvVar(_)
            | TermNode::BvUn(..)
            | TermNode::BvBin(..)
            | TermNode::BvIte(..) => Sort::Bv,
            TermNode::BoolConst(_)
            | TermNode::BoolUn(..)
            | TermNode::BoolBin(..)
            | TermNode::Rel(..) => Sort::Bool,
        }
    }
}

// Convenience constructors.
pub fn bv(n: u64) -> Term {
    Rc::new(TermNode::BvConst(n))
}
pub fn var(name: impl Into<String>) -> Term {
    Rc::new(TermNode::BvVar(name.into()))
}
pub fn bv_un(op: BvUnOp, a: Term) -> Term {
    Rc::new(TermNode::BvUn(op, a))
}
pub fn bv_bin(op: BvBinOp, a: Term, b: Term) -> Term {
    Rc::new(TermNode::BvBin(op, a, b))
}
pub fn ite(c: Term, t: Term, e: Term) -> Term {
    Rc::new(TermNode::BvIte(c, t, e))
}
pub fn boolc(b: bool) -> Term {
    Rc::new(TermNode::BoolConst(b))
}
pub fn not(a: Term) -> Term {
    Rc::new(TermNode::BoolUn(BoolUnOp::Not, a))
}
pub fn and(a: Term, b: Term) -> Term {
    Rc::new(TermNode::BoolBin(BoolBinOp::And, a, b))
}
pub fn or(a: Term, b: Term) -> Term {
    Rc::new(TermNode::BoolBin(BoolBinOp::Or, a, b))
}
pub fn implies(a: Term, b: Term) -> Term {
    Rc::new(TermNode::BoolBin(BoolBinOp::Implies, a, b))
}
pub fn rel(rel: CmpRel, a: Term, b: Term) -> Term {
    Rc::new(TermNode::Rel(rel, a, b))
}
pub fn nonzero(a: Term) -> Term {
    rel(CmpRel::Ne, a.clone(), bv(0))
}

impl CmpRel {
    pub fn is_equality(self) -> bool {
        matches!(self, CmpRel::Eq | CmpRel::Ne)
    }
}
