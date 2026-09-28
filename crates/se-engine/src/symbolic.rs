//! Symbolic expression evaluation: AST expressions become solver [`Term`]s while
//! semantic side-conditions (divisor non-zero, trap-mode overflow) are collected as
//! path guards.

use std::collections::BTreeMap;

use se_lang::ast::{BinOp, Expr, OverflowMode, Program, UnOp, Width};
use se_lang::interp::FailureKind;
use se_solver::term::{self, bv, bv_bin, bv_un, implies, ite as term_ite, nonzero, rel, Term};
use se_solver::{and as b_and, not as b_not, or as b_or};
use se_solver::BvBinOp;
use se_solver::CmpRel;

/// A semantic side-condition attached to a specific statement.
#[derive(Clone, Debug)]
pub struct Guard {
    pub kind: FailureKind,
    pub stmt_id: usize,
    pub op: &'static str,
    /// Boolean term that *must hold* on the path (e.g. divisor != 0).
    pub cond: Term,
}

#[derive(Clone)]
pub struct SymEval {
    pub term: Term,
    pub guards: Vec<Guard>,
}

pub struct SymbolicEvaluator<'a> {
    pub program: &'a Program,
    pub env: &'a BTreeMap<String, Term>,
}

impl<'a> SymbolicEvaluator<'a> {
    #[must_use]
    fn width(&self) -> Width {
        self.program.width
    }

    fn lift_binop(&self, op: BinOp) -> Result<BvBinOp, CmpRel> {
        Ok(match op {
            BinOp::Add => BvBinOp::Add,
            BinOp::Sub => BvBinOp::Sub,
            BinOp::Mul => BvBinOp::Mul,
            BinOp::Udiv => BvBinOp::Udiv,
            BinOp::Urem => BvBinOp::Urem,
            BinOp::Sdiv => BvBinOp::Sdiv,
            BinOp::Srem => BvBinOp::Srem,
            BinOp::And => BvBinOp::And,
            BinOp::Or => BvBinOp::Or,
            BinOp::Xor => BvBinOp::Xor,
            BinOp::Shl => BvBinOp::Shl,
            BinOp::LShr => BvBinOp::LShr,
            BinOp::AShr => BvBinOp::AShr,
            // Comparisons return a comparison relation instead.
            BinOp::Eq => return Err(CmpRel::Eq),
            BinOp::Ne => return Err(CmpRel::Ne),
            BinOp::Ult => return Err(CmpRel::Ult),
            BinOp::Ule => return Err(CmpRel::Ule),
            BinOp::Ugt => return Err(CmpRel::Ugt),
            BinOp::Uge => return Err(CmpRel::Uge),
            BinOp::Slt => return Err(CmpRel::Slt),
            BinOp::Sle => return Err(CmpRel::Sle),
            BinOp::Sgt => return Err(CmpRel::Sgt),
            BinOp::Sge => return Err(CmpRel::Sge),
        })
    }

    pub fn eval(&self, e: &Expr, stmt_id: usize) -> SymEval {
        let mut acc: Vec<Guard> = Vec::new();
        let t = self.eval_into(e, stmt_id, &mut acc);
        SymEval {
            term: t,
            guards: acc,
        }
    }

    fn eval_into(&self, e: &Expr, stmt_id: usize, acc: &mut Vec<Guard>) -> Term {
        match e {
            Expr::Int(n) => bv(n & self.width().mask_u64()),
            Expr::Var(name) => self
                .env
                .get(name)
                .cloned()
                .unwrap_or_else(|| bv(0)),
            Expr::Un { op, arg } => {
                let a = self.eval_into(arg, stmt_id, acc);
                let sop = match op {
                    UnOp::Neg => term::BvUnOp::Neg,
                    UnOp::Not => term::BvUnOp::Not,
                };
                let result = bv_un(sop, a.clone());
                if self.program.overflow == OverflowMode::Trap {
                    // -x traps when x == INT_MIN  <=>  sdiv edge case.
                    let int_min = bv(1u64 << (self.width().bits() - 1));
                    let safe = rel(CmpRel::Ne, a, int_min);
                    acc.push(Guard {
                        kind: FailureKind::Overflow,
                        stmt_id,
                        op: "neg",
                        cond: safe,
                    });
                }
                result
            }
            Expr::Bin { op, lhs, rhs } => {
                let a = self.eval_into(lhs, stmt_id, acc);
                let b = self.eval_into(rhs, stmt_id, acc);
                self.eval_binary(*op, a, b, stmt_id, acc)
            }
            Expr::Ite { cond, then, els } => {
                let c = self.eval_into(cond, stmt_id, acc);
                let cbool = nonzero(c);
                // Evaluate each branch into its own guard buffer; only the branch
                // selected by the condition can actually fail, so its guards are
                // weakened by the branch condition (`cb => g` for the then branch,
                // `¬cb => g` for the else branch).
                let mut then_guards: Vec<Guard> = Vec::new();
                let t = self.eval_into(then, stmt_id, &mut then_guards);
                let mut else_guards: Vec<Guard> = Vec::new();
                let e2 = self.eval_into(els, stmt_id, &mut else_guards);
                for g in then_guards {
                    acc.push(Guard {
                        cond: implies(cbool.clone(), g.cond),
                        ..g
                    });
                }
                for g in else_guards {
                    acc.push(Guard {
                        cond: implies(term::not(cbool.clone()), g.cond),
                        ..g
                    });
                }
                term_ite(cbool, t, e2)
            }
        }
    }

    fn eval_binary(
        &self,
        op: BinOp,
        a: Term,
        b: Term,
        stmt_id: usize,
        acc: &mut Vec<Guard>,
    ) -> Term {
        // Comparison operations produce Bool-derived 0/1 bitvectors.
        let cmp = self.lift_binop(op);
        match cmp {
            Err(rel_kind) => {
                let c = rel(rel_kind, a, b);
                // (ite c 1 0)
                term_ite(c, bv(1), bv(0))
            }
            Ok(bop) => {
                if op.is_divish() {
                    // Guard: divisor != 0 (left operand of guard is the divisor `b`).
                    acc.push(Guard {
                        kind: FailureKind::DivByZero,
                        stmt_id,
                        op: op.as_str(),
                        cond: nonzero(b.clone()),
                    });
                }
                if self.program.overflow == OverflowMode::Trap {
                    if let Some(g) = self.trap_guard(op, a.clone(), b.clone(), stmt_id) {
                        acc.push(g);
                    }
                }
                bv_bin(bop, a, b)
            }
        }
    }

    /// Construct the no-overflow guard for trapping arithmetic. Guard holds iff safe.
    fn trap_guard(
        &self,
        op: BinOp,
        a: Term,
        b: Term,
        stmt_id: usize,
    ) -> Option<Guard> {
        let w = self.width();
        let cond = match op {
            BinOp::Add => {
                // Unsigned wrap detection: a + b carries iff (~a) <u b.
                let not_a = bv_un(term::BvUnOp::Not, a.clone());
                rel(CmpRel::Uge, not_a, b.clone())
            }
            BinOp::Sub => {
                // Borrow iff a <u b.
                rel(CmpRel::Uge, a.clone(), b.clone())
            }
            BinOp::Mul => {
                // Overflow check via the classic identity b == 0 or a <= MAX / b.
                let max = bv(w.mask_u64());
                let zero = bv(0);
                let b_zero = rel(CmpRel::Eq, b.clone(), zero);
                let quotient = bv_bin(BvBinOp::Udiv, max, b.clone());
                let fits = rel(CmpRel::Ule, a.clone(), quotient);
                b_or(b_zero, fits)
            }
            BinOp::Sdiv | BinOp::Srem => {
                // Traps only on INT_MIN / -1.
                let int_min = bv(1u64 << (w.bits() - 1));
                let minus_one = bv(w.mask_u64());
                let edge = b_and(
                    rel(CmpRel::Eq, a, int_min),
                    rel(CmpRel::Eq, b, minus_one),
                );
                b_not(edge)
            }
            _ => return None,
        };
        Some(Guard {
            kind: FailureKind::Overflow,
            stmt_id,
            op: op.as_str(),
            cond,
        })
    }
}
