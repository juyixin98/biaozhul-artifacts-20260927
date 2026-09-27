//! Native path-condition evaluator.
//!
//! The symbolic kernel builds, alongside every Z3 expression, a plain tree in
//! [`NInt`]/[`NBool`] describing the same constraint. This module evaluates
//! those trees with hand-written recursion and no solver involvement, which
//! gives the test suite a second way to decide "which inputs reach this
//! failure" over a small domain. Crucially, the enumeration and the answers
//! are produced here, not by the kernel: the kernel only supplies the
//! constraint text.

use std::collections::HashMap;

use serde::Serialize;

use crate::evidence::concrete::ConcreteInput;
use crate::evidence::sem;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum NUnOp {
    Neg,
    BitNot,
    Not,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
pub enum NBinOp {
    Add,
    Sub,
    Mul,
    Div,
    Rem,
    BitAnd,
    BitOr,
    BitXor,
    Shl,
    Shr,
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    LAnd,
    LOr,
}

/// Integer term. Every node carries its own fixed bit width, matching the
/// type-checked AST, so wrapping is applied exactly as the concrete machine
/// would apply it.
#[derive(Debug, Clone, Serialize)]
pub enum NInt {
    /// (width, value)
    Const(u32, u64),
    /// (width, parameter name) — program input, always version 0.
    Param(u32, String),
    /// (width, variable name, SSA version)
    Ssa(u32, String, u32),
    Un(u32, NUnOp, Box<NInt>),
    Bin(u32, NBinOp, Box<NInt>, Box<NInt>),
}

impl NInt {
    pub fn width(&self) -> u32 {
        match self {
            NInt::Const(w, _) | NInt::Param(w, _) | NInt::Ssa(w, _, _) | NInt::Un(w, _, _) | NInt::Bin(w, _, _, _) => *w,
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub enum NBool {
    Const(bool),
    Not(Box<NBool>),
    LAnd(Box<NBool>, Box<NBool>),
    LOr(Box<NBool>, Box<NBool>),
    Cmp(NBinOp, Box<NInt>, Box<NInt>),
    /// Divisor-safety guard: the inner integer must be nonzero.
    NonZero(Box<NInt>),
}

/// SSA environment: `"x#3"` -> native integer expression bound at that version.
pub type SsaEnv = HashMap<String, NInt>;

#[derive(Debug, Clone)]
pub enum NativeEvalError {
    UnresolvedSsa(String),
    UnresolvedParam(String),
    SuspectedCycle(String),
    WidthMismatch { op: NBinOp, left: u32, right: u32 },
    DomainTooLarge(u128, u128),
}

impl std::fmt::Display for NativeEvalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            NativeEvalError::UnresolvedSsa(n) => write!(f, "unresolved SSA name `{n}`"),
            NativeEvalError::UnresolvedParam(n) => write!(f, "unresolved parameter `{n}`"),
            NativeEvalError::SuspectedCycle(n) => write!(f, "cycle suspected resolving `{n}`"),
            NativeEvalError::WidthMismatch { op, left, right } => {
                write!(f, "width mismatch for {op:?}: {left} vs {right}")
            }
            NativeEvalError::DomainTooLarge(need, cap) => {
                write!(f, "exhaustive domain has {need} assignments > cap {cap}")
            }
        }
    }
}
impl std::error::Error for NativeEvalError {}

pub struct Evaluator<'a> {
    ssa: &'a SsaEnv,
    inputs: &'a ConcreteInput,
    resolving: Vec<String>,
}

impl<'a> Evaluator<'a> {
    fn int(&mut self, e: &NInt) -> Result<u64, NativeEvalError> {
        let width = e.width();
        let v = match e {
            NInt::Const(_, v) => *v,
            NInt::Param(_, name) => *self
                .inputs
                .get(name)
                .ok_or_else(|| NativeEvalError::UnresolvedParam(name.clone()))?,
            NInt::Ssa(_, name, ver) => {
                let key = ssa_key(name, *ver);
                if self.resolving.contains(&key) {
                    return Err(NativeEvalError::SuspectedCycle(key));
                }
                self.resolving.push(key.clone());
                let bound = self
                    .ssa
                    .get(&key)
                    .cloned()
                    .ok_or_else(|| NativeEvalError::UnresolvedSsa(key.clone()))?;
                let v = self.int(&bound)?;
                self.resolving.pop();
                v
            }
            NInt::Un(_, op, inner) => {
                let v = self.int(inner)?;
                match op {
                    NUnOp::Neg => sem::neg(width, v),
                    NUnOp::BitNot => sem::bitnot(width, v),
                    NUnOp::Not => unreachable!("logical not on integer"),
                }
            }
            NInt::Bin(_, op, a, b) => {
                let av = self.int(a)?;
                let bv = self.int(b)?;
                if a.width() != width || b.width() != width {
                    return Err(NativeEvalError::WidthMismatch {
                        op: *op,
                        left: a.width(),
                        right: b.width(),
                    });
                }
                match op {
                    NBinOp::Add => sem::add(width, av, bv),
                    NBinOp::Sub => sem::sub(width, av, bv),
                    NBinOp::Mul => sem::mul(width, av, bv),
                    NBinOp::Div => av.checked_div(bv).unwrap_or(0),
                    NBinOp::Rem => av.checked_rem(bv).unwrap_or(0),
                    NBinOp::BitAnd => av & bv,
                    NBinOp::BitOr => av | bv,
                    NBinOp::BitXor => av ^ bv,
                    NBinOp::Shl => sem::shl(width, av, bv),
                    NBinOp::Shr => sem::shr(width, av, bv),
                    _ => unreachable!("comparison on integer path"),
                }
            }
        };
        Ok(v & sem_mask(width))
    }

    fn bool_(&mut self, e: &NBool) -> Result<bool, NativeEvalError> {
        Ok(match e {
            NBool::Const(b) => *b,
            NBool::Not(inner) => !self.bool_(inner)?,
            NBool::LAnd(a, b) => self.bool_(a)? && self.bool_(b)?,
            NBool::LOr(a, b) => self.bool_(a)? || self.bool_(b)?,
            NBool::Cmp(op, a, b) => {
                if a.width() != b.width() {
                    return Err(NativeEvalError::WidthMismatch {
                        op: *op,
                        left: a.width(),
                        right: b.width(),
                    });
                }
                let av = self.int(a)?;
                let bv = self.int(b)?;
                match op {
                    NBinOp::Eq => av == bv,
                    NBinOp::Ne => av != bv,
                    NBinOp::Lt => av < bv,
                    NBinOp::Le => av <= bv,
                    NBinOp::Gt => av > bv,
                    NBinOp::Ge => av >= bv,
                    _ => unreachable!(),
                }
            }
            NBool::NonZero(inner) => self.int(inner)? != 0,
        })
    }
}

fn sem_mask(bits: u32) -> u64 {
    if bits == 64 {
        u64::MAX
    } else {
        (1u64 << bits) - 1
    }
}

pub fn ssa_key(name: &str, ver: u32) -> String {
    format!("{name}#{ver}")
}

/// Evaluate a conjunction of boolean constraints for one concrete input.
pub fn eval_pc(pc: &[NBool], ssa: &SsaEnv, input: &ConcreteInput) -> Result<bool, NativeEvalError> {
    let mut ev = Evaluator {
        ssa,
        inputs: input,
        resolving: Vec::new(),
    };
    for c in pc {
        if !ev.bool_(c)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// Exhaustively evaluate a conjunction over a bounded rectangular domain.
///
/// `params` lists `(name, bits)`; the domain is the Cartesian product of
/// `0..2^bits`. `ssa` supplies intermediate bindings produced by the kernel.
/// Returns an error instead of enumerating if the product exceeds `cap`
/// assignments, so callers never silently pay a huge cost.
pub fn eval_pc_over_domain(
    pc: &[NBool],
    ssa: &SsaEnv,
    params: &[(String, u32)],
    cap: u128,
) -> Result<Vec<ConcreteInput>, NativeEvalError> {
    let total: u128 = params
        .iter()
        .fold(1u128, |acc, (_, b)| acc * (1u128 << b));
    if total > cap {
        return Err(NativeEvalError::DomainTooLarge(total, cap));
    }
    let mut out = Vec::new();
    let n = total as u64;
    for mixed in 0..n {
        let mut rest = mixed;
        let mut input = ConcreteInput::new();
        for (name, bits) in params.iter() {
            let v = rest & ((1u64 << bits) - 1);
            rest /= 1u64 << bits;
            input.insert(name.clone(), v);
        }
        if eval_pc(pc, ssa, &input)? {
            out.push(input);
        }
    }
    Ok(out)
}
