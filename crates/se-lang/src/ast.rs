//! Lowered, validated program representation.
//!
//! Programs reach this representation through [`crate::dto::ProgramDto::lower`], which
//! assigns every statement a unique [`Stmt::id`] (pre-order, starting at 0) and
//! rejects malformed programs.  Other crates only ever see this validated form.

use std::rc::Rc;

/// Supported fixed integer widths, in bits.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Width {
    W8 = 8,
    W16 = 16,
    W32 = 32,
    W64 = 64,
}

impl Width {
    pub fn bits(self) -> u32 {
        self as u32
    }

    pub fn bytes(self) -> u32 {
        self.bits() / 8
    }

    pub fn from_bits(bits: u64) -> Option<Width> {
        match bits {
            8 => Some(Width::W8),
            16 => Some(Width::W16),
            32 => Some(Width::W32),
            64 => Some(Width::W64),
            _ => None,
        }
    }

    /// Mask retaining exactly `bits` low bits of a u64.
    pub fn mask_u64(self) -> u64 {
        if self.bits() == 64 {
            u64::MAX
        } else {
            (1u64 << self.bits()) - 1
        }
    }
}

/// What happens when an arithmetic operation overflows the fixed width.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OverflowMode {
    /// Two's-complement wrap-around (default).
    Wrap,
    /// Overflow (and signed division edge cases) are a runtime failure.
    Trap,
}

/// Symbolic input declared by the program.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Input {
    pub name: String,
    /// Inclusive lower bound (unsigned w-bit domain value).
    pub low: u64,
    /// Inclusive upper bound (unsigned w-bit domain value).
    pub high: u64,
}

/// Optional pre-declared state variable with a concrete initial value.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Var {
    pub name: String,
    pub init: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum BinOp {
    // --- arithmetic ---
    Add,
    Sub,
    Mul,
    Udiv,
    Urem,
    Sdiv,
    Srem,
    // --- bitwise ---
    And,
    Or,
    Xor,
    Shl,
    LShr,
    AShr,
    // --- unsigned comparisons (result is 0/1) ---
    Eq,
    Ne,
    Ult,
    Ule,
    Ugt,
    Uge,
    // --- signed comparisons ---
    Slt,
    Sle,
    Sgt,
    Sge,
}

impl BinOp {
    pub fn as_str(self) -> &'static str {
        match self {
            BinOp::Add => "add",
            BinOp::Sub => "sub",
            BinOp::Mul => "mul",
            BinOp::Udiv => "udiv",
            BinOp::Urem => "urem",
            BinOp::Sdiv => "sdiv",
            BinOp::Srem => "srem",
            BinOp::And => "and",
            BinOp::Or => "or",
            BinOp::Xor => "xor",
            BinOp::Shl => "shl",
            BinOp::LShr => "lshr",
            BinOp::AShr => "ashr",
            BinOp::Eq => "eq",
            BinOp::Ne => "ne",
            BinOp::Ult => "ult",
            BinOp::Ule => "ule",
            BinOp::Ugt => "ugt",
            BinOp::Uge => "uge",
            BinOp::Slt => "slt",
            BinOp::Sle => "sle",
            BinOp::Sgt => "sgt",
            BinOp::Sge => "sge",
        }
    }

    /// Division/remainder operations whose divisor must be non-zero.
    pub fn is_divish(self) -> bool {
        matches!(
            self,
            BinOp::Udiv | BinOp::Urem | BinOp::Sdiv | BinOp::Srem
        )
    }

    /// Trapping arithmetic operations (under [`OverflowMode::Trap`]).
    pub fn is_trapping_arith(self) -> bool {
        matches!(self, BinOp::Add | BinOp::Sub | BinOp::Mul)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UnOp {
    /// Two's-complement negation.
    Neg,
    /// Bitwise complement.
    Not,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Expr {
    /// A w-bit literal (always masked to the program width).
    Int(u64),
    Var(String),
    Bin {
        op: BinOp,
        lhs: Box<Expr>,
        rhs: Box<Expr>,
    },
    Un {
        op: UnOp,
        arg: Box<Expr>,
    },
    /// Result is 1 when `cond` is non-zero else `els`, all operands w-bit.
    Ite {
        cond: Box<Expr>,
        then: Box<Expr>,
        els: Box<Expr>,
    },
}

/// A block is a shared (`Rc`) statement vector so AST nodes can reference sub-blocks
/// cheaply and the engine can store continuation block references.
pub type Block = Rc<Vec<Stmt>>;

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Stmt {
    Assign {
        id: usize,
        target: String,
        expr: Expr,
    },
    If {
        id: usize,
        cond: Expr,
        then_blk: Block,
        else_blk: Block,
    },
    While {
        id: usize,
        cond: Expr,
        body: Block,
    },
    /// Feasibility assumption: a path where `cond` is zero cannot be taken.
    Assume { id: usize, cond: Expr },
    /// Safety assertion: a zero `cond` is a violation.
    Assert { id: usize, cond: Expr },
}

impl Stmt {
    pub fn id(&self) -> usize {
        match self {
            Stmt::Assign { id, .. }
            | Stmt::If { id, .. }
            | Stmt::While { id, .. }
            | Stmt::Assume { id, .. }
            | Stmt::Assert { id, .. } => *id,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Program {
    pub width: Width,
    pub overflow: OverflowMode,
    pub inputs: Vec<Input>,
    pub vars: Vec<Var>,
    pub body: Block,
    /// Number of statements in the program, i.e. ids range over `0..stmt_count`.
    pub stmt_count: usize,
}

impl Program {
    pub fn input_names(&self) -> Vec<&str> {
        self.inputs.iter().map(|i| i.name.as_str()).collect()
    }

    pub fn find_input(&self, name: &str) -> Option<&Input> {
        self.inputs.iter().find(|i| i.name == name)
    }

    pub fn is_input(&self, name: &str) -> bool {
        self.find_input(name).is_some()
    }
}
