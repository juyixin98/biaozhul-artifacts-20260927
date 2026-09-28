//! Concrete interpreter for the small integer language.
//!
//! Semantics deliberately match the *bounded* analysis: integers are i64 and
//! `+`, `-`, `*`, unary `-` are checked, producing [`ConcErrorKind::Overflow`]
//! instead of wrapping. Array indexing is checked against the declared
//! length. This interpreter is the independent reference used by the
//! exhaustive soundness tests: it shares no code with the interval engine.

use crate::lang::{CmpOp, Cond, Expr, Span, Stmt};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ConcErrorKind {
    Overflow,
    OutOfBounds {
        index: i64,
        len: usize,
    },
    AssertFailed,
    FuelExhausted,
    InvalidInput {
        name: String,
        value: i64,
        lo: i64,
        hi: i64,
    },
    /// Name/shape problems should already have been rejected by `validate`;
    /// encountering one here is a test-harness bug, but keeping it closed.
    IllFormed(String),
}

#[derive(Debug, Clone)]
pub struct ConcError {
    pub kind: ConcErrorKind,
    pub span: Span,
}

#[derive(Debug, Clone, Default)]
pub struct ConcreteObs {
    /// Min/max value assigned at each assignment statement (keyed by offset).
    pub assigns: BTreeMap<u32, (i64, i64)>,
    /// Min/max value ever stored into each array.
    pub array_stored: BTreeMap<String, (i64, i64)>,
}

#[derive(Debug, Clone)]
pub struct RunResult {
    pub final_vars: BTreeMap<String, i64>,
    pub final_arrays: BTreeMap<String, Vec<i64>>,
    pub obs: ConcreteObs,
    pub error: Option<ConcError>,
}

fn update_minmax(m: &mut BTreeMap<u32, (i64, i64)>, key: u32, v: i64) {
    m.entry(key)
        .and_modify(|(lo, hi)| {
            *lo = (*lo).min(v);
            *hi = (*hi).max(v);
        })
        .or_insert((v, v));
}

fn update_named_minmax(m: &mut BTreeMap<String, (i64, i64)>, key: &str, v: i64) {
    m.entry(key.to_string())
        .and_modify(|(lo, hi)| {
            *lo = (*lo).min(v);
            *hi = (*hi).max(v);
        })
        .or_insert((v, v));
}

pub fn run(program: &[Stmt], inputs: &BTreeMap<String, i64>, fuel: usize) -> RunResult {
    let mut vm = Vm {
        vars: BTreeMap::new(),
        arrays: BTreeMap::new(),
        obs: ConcreteObs::default(),
        fuel,
        declared_inputs: BTreeSet::new(),
        error: None,
    };
    vm.exec_block(program, inputs);
    RunResult {
        final_vars: vm.vars,
        final_arrays: vm.arrays,
        obs: vm.obs,
        error: vm.error,
    }
}

struct Vm {
    vars: BTreeMap<String, i64>,
    arrays: BTreeMap<String, Vec<i64>>,
    obs: ConcreteObs,
    fuel: usize,
    declared_inputs: BTreeSet<String>,
    error: Option<ConcError>,
}

impl Vm {
    fn tick(&mut self) -> bool {
        if self.fuel == 0 {
            return false;
        }
        self.fuel -= 1;
        true
    }

    fn fail(&mut self, kind: ConcErrorKind, span: Span) {
        self.error = Some(ConcError { kind, span });
    }

    fn exec_block(&mut self, stmts: &[Stmt], inputs: &BTreeMap<String, i64>) {
        for s in stmts {
            if self.error.is_some() {
                return;
            }
            self.exec_stmt(s, inputs);
        }
    }

    fn exec_stmt(&mut self, s: &Stmt, inputs: &BTreeMap<String, i64>) {
        if !self.tick() {
            self.fail(ConcErrorKind::FuelExhausted, s.span());
            return;
        }
        match s {
            Stmt::Input { name, lo, hi, span } => {
                self.declared_inputs.insert(name.clone());
                let v = match inputs.get(name) {
                    Some(v) => *v,
                    None => {
                        self.fail(
                            ConcErrorKind::IllFormed(format!("missing concrete input `{name}`")),
                            *span,
                        );
                        return;
                    }
                };
                if v < *lo || v > *hi {
                    self.fail(
                        ConcErrorKind::InvalidInput {
                            name: name.clone(),
                            value: v,
                            lo: *lo,
                            hi: *hi,
                        },
                        *span,
                    );
                    return;
                }
                self.vars.insert(name.clone(), v);
            }
            Stmt::ArrayDecl { name, len, .. } => {
                self.arrays.insert(name.clone(), vec![0i64; *len]);
            }
            Stmt::Assign { name, expr, span } => {
                if let Some(v) = self.eval(expr) {
                    update_minmax(&mut self.obs.assigns, span.offset, v);
                    self.vars.insert(name.clone(), v);
                }
            }
            Stmt::ArrayStore {
                name,
                index,
                value,
                span,
            } => {
                let idx = match self.eval(index) {
                    Some(v) => v,
                    None => return,
                };
                let v = match self.eval(value) {
                    Some(v) => v,
                    None => return,
                };
                let arr = match self.arrays.get(name.as_str()) {
                    Some(a) => a,
                    None => {
                        self.fail(
                            ConcErrorKind::IllFormed(format!("unknown array `{name}`")),
                            *span,
                        );
                        return;
                    }
                };
                if idx < 0 || idx as usize >= arr.len() {
                    self.fail(
                        ConcErrorKind::OutOfBounds {
                            index: idx,
                            len: arr.len(),
                        },
                        index.span(),
                    );
                    return;
                }
                update_named_minmax(&mut self.obs.array_stored, name, v);
                self.arrays.get_mut(name.as_str()).unwrap()[idx as usize] = v;
            }
            Stmt::If {
                cond,
                then_body,
                else_body,
                ..
            } => {
                if self.eval_cond(cond) {
                    self.exec_block(then_body, inputs);
                } else {
                    self.exec_block(else_body, inputs);
                }
            }
            Stmt::While {
                cond, body, span, ..
            } => {
                // Fuel bounds loop iterations as well as statements.
                loop {
                    if self.error.is_some() {
                        return;
                    }
                    if !self.tick() {
                        self.fail(ConcErrorKind::FuelExhausted, *span);
                        return;
                    }
                    if !self.eval_cond(cond) {
                        break;
                    }
                    self.exec_block(body, inputs);
                    if self.error.is_some() {
                        return;
                    }
                }
            }
            Stmt::Assert { cond, span } => {
                if !self.eval_cond(cond) {
                    self.fail(ConcErrorKind::AssertFailed, *span);
                }
            }
        }
    }

    fn eval(&mut self, e: &Expr) -> Option<i64> {
        match e {
            Expr::Int(v, _) => Some(*v),
            Expr::Var(name, span) => match self.vars.get(name) {
                Some(v) => Some(*v),
                None => {
                    self.fail(
                        ConcErrorKind::IllFormed(format!("undefined scalar `{name}`")),
                        *span,
                    );
                    None
                }
            },
            Expr::Load { array, index, span } => {
                let idx = self.eval(index)?;
                let arr = match self.arrays.get(array.as_str()) {
                    Some(a) => a,
                    None => {
                        self.fail(
                            ConcErrorKind::IllFormed(format!("unknown array `{array}`")),
                            *span,
                        );
                        return None;
                    }
                };
                if idx < 0 || idx as usize >= arr.len() {
                    self.fail(
                        ConcErrorKind::OutOfBounds {
                            index: idx,
                            len: arr.len(),
                        },
                        *span,
                    );
                    return None;
                }
                Some(arr[idx as usize])
            }
            Expr::Neg(inner, span) => {
                let v = self.eval(inner)?;
                match v.checked_neg() {
                    Some(r) => Some(r),
                    None => {
                        self.fail(ConcErrorKind::Overflow, *span);
                        None
                    }
                }
            }
            Expr::Add(l, r, span) => {
                let a = self.eval(l)?;
                let b = self.eval(r)?;
                match a.checked_add(b) {
                    Some(v) => Some(v),
                    None => {
                        self.fail(ConcErrorKind::Overflow, *span);
                        None
                    }
                }
            }
            Expr::Sub(l, r, span) => {
                let a = self.eval(l)?;
                let b = self.eval(r)?;
                match a.checked_sub(b) {
                    Some(v) => Some(v),
                    None => {
                        self.fail(ConcErrorKind::Overflow, *span);
                        None
                    }
                }
            }
            Expr::Mul(l, r, span) => {
                let a = self.eval(l)?;
                let b = self.eval(r)?;
                match a.checked_mul(b) {
                    Some(v) => Some(v),
                    None => {
                        self.fail(ConcErrorKind::Overflow, *span);
                        None
                    }
                }
            }
        }
    }

    fn eval_cond(&mut self, c: &Cond) -> bool {
        match c {
            Cond::Cmp { op, lhs, rhs, .. } => {
                // Both sides are evaluated for their side effects/errors; on
                // overflow the error is already recorded and this returns
                // false, which aborts the program on the next step anyway.
                let a = match self.eval(lhs) {
                    Some(v) => v,
                    None => return false,
                };
                let b = match self.eval(rhs) {
                    Some(v) => v,
                    None => return false,
                };
                match op {
                    CmpOp::Lt => a < b,
                    CmpOp::Le => a <= b,
                    CmpOp::Eq => a == b,
                    CmpOp::Ne => a != b,
                    CmpOp::Ge => a >= b,
                    CmpOp::Gt => a > b,
                }
            }
            Cond::And(a, b, _) => self.eval_cond(a) && self.eval_cond(b),
            Cond::Or(a, b, _) => self.eval_cond(a) || self.eval_cond(b),
            Cond::Not(a, _) => !self.eval_cond(a),
        }
    }
}
