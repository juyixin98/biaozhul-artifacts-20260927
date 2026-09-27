//! Independent concrete reference executor for IAL.
//!
//! This crate deliberately does **not** depend on `ia-solver` or
//! `ia-intervals`. It is a second, from-scratch implementation of the same
//! bounded semantics, so the differential tests in `ia-verify` compare two
//! independently written pieces of code instead of a core grading itself.
//!
//! Arithmetic uses Rust's `checked_*` i64 operations: any overflow is a
//! concrete [`ExecFailure`], never a wrapped value.
use ia_lang::ast::*;
use ia_lang::{ProgramInfo, Span};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Categories of concrete runtime failure. Integration tests assert on these
/// exact labels, so they are part of the public contract.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureKind {
    /// Checked i64 arithmetic overflow, including `-i64::MIN` and `MIN / -1`.
    Overflow,
    /// Division or modulo by zero.
    DivByZero,
    /// Array index outside `[0, len)`.
    IndexOutOfBounds,
    /// `assert(e)` observed `e == 0`.
    AssertionFailed,
    /// Concrete execution exceeded its step budget (guards the enumerator
    /// against a non-terminating program; analysis itself is step-bounded
    /// separately).
    StepLimit,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecFailure {
    pub kind: FailureKind,
    pub message: String,
    /// Byte offset of the expression/statement that failed; matches the
    /// abstract report's span offsets.
    pub at_offset: usize,
    pub line: u32,
    pub column: u32,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct PointObservation {
    /// Byte offset identifying the program point (shared with abstract
    /// trace steps).
    pub point_id: usize,
    /// Variable values live immediately *after* the point.
    pub vars: HashMap<String, i64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum RunOutcome {
    /// Program terminated normally.
    Normal {
        final_vars: HashMap<String, i64>,
        trace: Vec<PointObservation>,
        steps: u64,
    },
    Failed {
        failure: ExecFailure,
        trace: Vec<PointObservation>,
        steps: u64,
    },
}

impl RunOutcome {
    pub fn is_failed(&self) -> bool {
        matches!(self, RunOutcome::Failed { .. })
    }
    pub fn failure(&self) -> Option<&ExecFailure> {
        match self {
            RunOutcome::Failed { failure, .. } => Some(failure),
            _ => None,
        }
    }
    pub fn trace(&self) -> &[PointObservation] {
        match self {
            RunOutcome::Normal { trace, .. } | RunOutcome::Failed { trace, .. } => trace,
        }
    }
}

/// Default safety bound for one concrete execution. A program that needs more
/// steps is flagged `step_limit`, never silently treated as normal.
pub const DEFAULT_STEP_LIMIT: u64 = 1_000_000;

// The executor needs the program body, which lives on `Program`, while
// `ProgramInfo` only carries declarations; entry points take both.
/// Run one concrete execution with default step budget.
pub fn run_program(
    program: &Program,
    info: &ProgramInfo,
    source: &str,
    assignment: &HashMap<String, i64>,
) -> RunOutcome {
    run_program_bounded(program, info, source, assignment, DEFAULT_STEP_LIMIT)
}

pub fn run_program_bounded(
    program: &Program,
    info: &ProgramInfo,
    source: &str,
    assignment: &HashMap<String, i64>,
    step_limit: u64,
) -> RunOutcome {
    let mut ctx = Ctx {
        vars: HashMap::new(),
        arrays: HashMap::new(),
        trace: Vec::new(),
        steps: 0,
        step_limit,
    };
    let _ = source; // failures carry resolved line/column from the parser spans
    for (name, value) in &info.consts {
        ctx.vars.insert(name.clone(), *value);
    }
    for input in &info.inputs {
        let v = assignment.get(&input.name).copied().unwrap_or(input.lo);
        assert!(
            (input.lo..=input.hi).contains(&v),
            "input {}={} outside declared [{}, {}]",
            input.name,
            v,
            input.lo,
            input.hi
        );
        ctx.vars.insert(input.name.clone(), v);
    }
    for (name, arr) in &info.arrays {
        ctx.arrays.insert(name.clone(), vec![0i64; arr.len as usize]);
    }
    // Implicit scalars are zero-initialised before the body runs.
    for name in &info.scalars {
        ctx.vars.entry(name.clone()).or_insert(0);
    }
    match ctx.exec_block(&program.body, info) {
        Ok(()) => RunOutcome::Normal {
            final_vars: ctx.vars,
            trace: ctx.trace,
            steps: ctx.steps,
        },
        Err(f) => RunOutcome::Failed {
            failure: f,
            trace: ctx.trace,
            steps: ctx.steps,
        },
    }
}

struct Ctx {
    vars: HashMap<String, i64>,
    arrays: HashMap<String, Vec<i64>>,
    trace: Vec<PointObservation>,
    steps: u64,
    step_limit: u64,
}

impl Ctx {
    fn tick(&mut self, span: Span) -> Result<(), ExecFailure> {
        self.steps += 1;
        if self.steps > self.step_limit {
            Err(self.fail(FailureKind::StepLimit, "step limit exceeded", span))
        } else {
            Ok(())
        }
    }

    fn fail(&self, kind: FailureKind, message: &str, span: Span) -> ExecFailure {
        ExecFailure {
            kind,
            message: message.to_string(),
            at_offset: span.start.offset,
            line: span.start.line,
            column: span.start.column,
        }
    }

    fn snapshot(&mut self, span: Span) {
        self.trace.push(PointObservation {
            point_id: span.start.offset,
            vars: self.vars.clone(),
        });
    }

    fn exec_block(&mut self, b: &Block, info: &ProgramInfo) -> Result<(), ExecFailure> {
        for s in &b.stmts {
            self.exec_stmt(s, info)?;
        }
        Ok(())
    }

    fn exec_stmt(&mut self, s: &Stmt, info: &ProgramInfo) -> Result<(), ExecFailure> {
        self.tick(s.span())?;
        match s {
            Stmt::Block(b) => self.exec_block(b, info),
            Stmt::Skip { .. } => Ok(()),
            Stmt::Assign { target, value, span } => {
                let v = self.eval(value)?;
                match &target.index {
                    None => {
                        self.vars.insert(target.name.clone(), v);
                    }
                    Some(idx_expr) => {
                        let idx = self.eval(idx_expr)?;
                        let arr = self
                            .arrays
                            .get_mut(&target.name)
                            .expect("validated array name");
                        if idx < 0 || (idx as u64) >= arr.len() as u64 {
                            return Err(self.fail(
                                FailureKind::IndexOutOfBounds,
                                "array index out of bounds",
                                idx_expr.span,
                            ));
                        }
                        arr[idx as usize] = v;
                    }
                }
                self.snapshot(*span);
                Ok(())
            }
            Stmt::If {
                cond,
                then,
                otherwise,
                ..
            } => {
                let c = self.eval(cond)?;
                if c != 0 {
                    self.exec_stmt(then, info)
                } else if let Some(e) = otherwise {
                    self.exec_stmt(e, info)
                } else {
                    Ok(())
                }
            }
            Stmt::While { cond, body, .. } => {
                loop {
                    let c = self.eval(cond)?;
                    if c == 0 {
                        break;
                    }
                    self.exec_stmt(body, info)?;
                }
                Ok(())
            }
            Stmt::Assert { cond, span } => {
                let c = self.eval(cond)?;
                if c == 0 {
                    Err(self.fail(FailureKind::AssertionFailed, "assertion failed", *span))
                } else {
                    Ok(())
                }
            }
        }
    }

    fn eval(&mut self, e: &Expr) -> Result<i64, ExecFailure> {
        match &e.kind {
            ExprKind::Int(v) => Ok(*v),
            ExprKind::Var(name) => Ok(*self
                .vars
                .get(name)
                .expect("validated, declared scalar")),
            ExprKind::ArrayRead { name, index } => {
                let idx = self.eval(index)?;
                let arr = self
                    .arrays
                    .get(name)
                    .expect("validated array name");
                if idx < 0 || (idx as u64) >= arr.len() as u64 {
                    return Err(self.fail(
                        FailureKind::IndexOutOfBounds,
                        "array index out of bounds",
                        index.span,
                    ));
                }
                Ok(arr[idx as usize])
            }
            ExprKind::Unary { op, inner } => {
                let v = self.eval(inner)?;
                match op {
                    UnOp::Neg => v.checked_neg().ok_or_else(|| {
                        self.fail(FailureKind::Overflow, "negation overflow", e.span)
                    }),
                    UnOp::Not => Ok(if v == 0 { 1 } else { 0 }),
                }
            }
            ExprKind::Binary { op, lhs, rhs } => {
                // Logical operators never evaluate failures away: both
                // operands are evaluated (no short circuit), matching abstract
                // semantics.
                let a = self.eval(lhs)?;
                let b = self.eval(rhs)?;
                match op {
                    BinOp::Add => a.checked_add(b).ok_or_else(|| {
                        self.fail(FailureKind::Overflow, "addition overflow", e.span)
                    }),
                    BinOp::Sub => a.checked_sub(b).ok_or_else(|| {
                        self.fail(FailureKind::Overflow, "subtraction overflow", e.span)
                    }),
                    BinOp::Mul => a.checked_mul(b).ok_or_else(|| {
                        self.fail(FailureKind::Overflow, "multiplication overflow", e.span)
                    }),
                    BinOp::Div => {
                        if b == 0 {
                            return Err(self.fail(
                                FailureKind::DivByZero,
                                "division by zero",
                                rhs.span,
                            ));
                        }
                        a.checked_div(b).ok_or_else(|| {
                            self.fail(FailureKind::Overflow, "division overflow (MIN / -1)", e.span)
                        })
                    }
                    BinOp::Mod => {
                        if b == 0 {
                            return Err(self.fail(
                                FailureKind::DivByZero,
                                "modulo by zero",
                                rhs.span,
                            ));
                        }
                        a.checked_rem(b).ok_or_else(|| {
                            self.fail(FailureKind::Overflow, "remainder overflow", e.span)
                        })
                    }
                    BinOp::Lt => Ok((a < b) as i64),
                    BinOp::Le => Ok((a <= b) as i64),
                    BinOp::Gt => Ok((a > b) as i64),
                    BinOp::Ge => Ok((a >= b) as i64),
                    BinOp::Eq => Ok((a == b) as i64),
                    BinOp::Ne => Ok((a != b) as i64),
                    BinOp::And => Ok((a != 0 && b != 0) as i64),
                    BinOp::Or => Ok((a != 0 || b != 0) as i64),
                }
            }
        }
    }
}

/// Enumerate every input assignment as a sequence of maps. `cap` bounds the
/// number of points produced; the caller reports `not_run` if exceeded.
pub fn enumerate_inputs(
    info: &ProgramInfo,
    cap: u64,
) -> Result<Vec<HashMap<String, i64>>, EnumerationLimit> {
    let mut total: u128 = 1;
    for input in &info.inputs {
        let count = (input.hi as i128 - input.lo as i128 + 1) as u128;
        total = total.saturating_mul(count);
        if total > cap as u128 {
            return Err(EnumerationLimit {
                combinations: total,
                cap,
            });
        }
    }
    if info.inputs.is_empty() {
        return Ok(vec![HashMap::new()]);
    }
    let mut out: Vec<HashMap<String, i64>> = Vec::with_capacity(total as usize);
    out.push(HashMap::new());
    for input in &info.inputs {
        let mut next = Vec::with_capacity(out.len() * (input.hi - input.lo + 1) as usize);
        for existing in &out {
            for v in input.lo..=input.hi {
                let mut m = existing.clone();
                m.insert(input.name.clone(), v);
                next.push(m);
            }
        }
        out = next;
    }
    Ok(out)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct EnumerationLimit {
    pub combinations: u128,
    pub cap: u64,
}
