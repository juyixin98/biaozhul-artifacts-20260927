//! Independent concrete interpreter — a reference semantics that never touches
//! the SMT solver. It is used both to replay counterexamples produced by the
//! kernel and to provide exhaustive oracle answers in tests.
//!
//! The interpreter records its branch/assert history as a list of steps keyed
//! by the same stable node ids the symbolic engine uses, so a replay can prove
//! that a counterexample reaches the *same* failure site through the same
//! branch choices.

use std::collections::HashMap;

use serde::Serialize;

use crate::lang::ast::*;
use crate::lang::types::Type;

use super::sem;

/// Concrete input assignment: parameter name -> concrete value.
pub type ConcreteInput = HashMap<String, u64>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureKind {
    /// An `assert(...)` evaluated to false.
    AssertionFailed,
    /// Division or remainder by zero.
    DivisionByZero,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum StepKind {
    Branch,
    Assert,
}

#[derive(Debug, Clone, Serialize)]
pub struct ConcreteStep {
    pub node_id: u32,
    pub kind: StepKind,
    pub line: u32,
    /// For branches: whether the condition evaluated true (then-branch taken).
    pub taken: Option<bool>,
    /// For assertions: whether it passed.
    pub passed: Option<bool>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConcreteOutcome {
    /// The program completed (all assertions encountered held).
    Completed,
    /// Execution stopped at a failing site.
    Failed,
}

#[derive(Debug, Clone, Serialize)]
pub struct ConcreteResult {
    pub outcome: ConcreteOutcome,
    pub failure: Option<FailureSite>,
    pub steps: Vec<ConcreteStep>,
}

#[derive(Debug, Clone, Serialize)]
pub struct FailureSite {
    pub kind: FailureKind,
    /// Node id of the failing assert / divisor expression.
    pub node_id: u32,
    pub line: u32,
    pub message: Option<String>,
}

/// Error raised when the concrete harness is misused (never for program
/// failures, which are reported as [`ConcreteOutcome::Failed`]).
#[derive(Debug, Clone)]
pub enum ConcreteError {
    MissingInput(String),
    InputOutOfRange { name: String, ty: Type, value: u64 },
    ExtraInput(String),
}

impl std::fmt::Display for ConcreteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConcreteError::MissingInput(n) => write!(f, "missing concrete input for parameter `{n}`"),
            ConcreteError::InputOutOfRange { name, ty, value } => write!(
                f,
                "input {value} for `{name}` does not fit in {}",
                ty.name()
            ),
            ConcreteError::ExtraInput(n) => write!(f, "concrete input `{n}` is not a program parameter"),
        }
    }
}
impl std::error::Error for ConcreteError {}

#[derive(Clone, Copy)]
enum Val {
    Int(u64),
    Bool(bool),
}

struct Interp<'a> {
    frames: Vec<HashMap<String, u64>>,
    steps: Vec<ConcreteStep>,
    _program: &'a Program,
}

impl<'a> Interp<'a> {
    fn lookup(&self, name: &str) -> Option<u64> {
        for f in self.frames.iter().rev() {
            if let Some(v) = f.get(name) {
                return Some(*v);
            }
        }
        None
    }
    fn assign(&mut self, name: &str, v: u64) {
        for f in self.frames.iter_mut().rev() {
            if f.contains_key(name) {
                f.insert(name.to_string(), v);
                return;
            }
        }
    }

    fn block(&mut self, stmts: &[Stmt]) -> Result<(), FailureSite> {
        self.frames.push(HashMap::new());
        for s in stmts {
            self.stmt(s)?;
        }
        self.frames.pop();
        Ok(())
    }

    fn stmt(&mut self, s: &Stmt) -> Result<(), FailureSite> {
        match &s.kind {
            StmtKind::Let { name, value, .. } => {
                let v = self.eval_int(value)?;
                self.frames.last_mut().unwrap().insert(name.clone(), v);
            }
            StmtKind::Assign { name, value } => {
                let v = self.eval_int(value)?;
                self.assign(name, v);
            }
            StmtKind::Assert { cond, message } => {
                let ok = self.eval_bool(cond)?;
                self.steps.push(ConcreteStep {
                    node_id: s.id.get(),
                    kind: StepKind::Assert,
                    line: s.span.line,
                    taken: None,
                    passed: Some(ok),
                });
                if !ok {
                    return Err(FailureSite {
                        kind: FailureKind::AssertionFailed,
                        node_id: s.id.get(),
                        line: s.span.line,
                        message: message.clone(),
                    });
                }
            }
            StmtKind::Assume { cond } => {
                // A false assumption on a concrete run means this input is
                // outside the assumed region; treat it as normal completion
                // (the path does not apply). Callers doing exhaustive search
                // consult only Failed outcomes, so this is conservative-safe.
                if !self.eval_bool(cond)? {
                    return Ok(());
                }
            }
            StmtKind::If { cond, then, els } => {
                let v = self.eval_bool(cond)?;
                self.steps.push(ConcreteStep {
                    node_id: s.id.get(),
                    kind: StepKind::Branch,
                    line: s.span.line,
                    taken: Some(v),
                    passed: None,
                });
                if v {
                    self.block(then)?;
                } else {
                    self.block(els)?;
                }
            }
            StmtKind::While { cond, body } => {
                while self.eval_bool(cond)? {
                    self.steps.push(ConcreteStep {
                        node_id: s.id.get(),
                        kind: StepKind::Branch,
                        line: s.span.line,
                        taken: Some(true),
                        passed: None,
                    });
                    self.block(body)?;
                }
                self.steps.push(ConcreteStep {
                    node_id: s.id.get(),
                    kind: StepKind::Branch,
                    line: s.span.line,
                    taken: Some(false),
                    passed: None,
                });
            }
        }
        Ok(())
    }

    fn eval_bool(&mut self, e: &Expr) -> Result<bool, FailureSite> {
        match self.eval(e)? {
            Val::Bool(b) => Ok(b),
            Val::Int(_) => panic!("type checker should have prevented integer in boolean position"),
        }
    }
    fn eval_int(&mut self, e: &Expr) -> Result<u64, FailureSite> {
        match self.eval(e)? {
            Val::Int(v) => Ok(v),
            Val::Bool(_) => panic!("type checker should have prevented boolean in integer position"),
        }
    }

    fn eval(&mut self, e: &Expr) -> Result<Val, FailureSite> {
        let v = match &e.kind {
            ExprKind::Lit(lit) => Val::Int(lit.value & lit.suffix.unwrap_or_else(|| e.ty.unwrap()).mask()),
            ExprKind::BoolLit(b) => Val::Bool(*b),
            ExprKind::Var(name) => Val::Int(self.lookup(name).expect("type-checked variable")),
            ExprKind::Un(op, inner) => {
                let bits = e.ty.map(|t| t.bits()).unwrap_or(0);
                match op {
                    UnOp::Neg => Val::Int(sem::neg(bits, self.eval_int(inner)?)),
                    UnOp::BitNot => Val::Int(sem::bitnot(bits, self.eval_int(inner)?)),
                    UnOp::Not => Val::Bool(!self.eval_bool(inner)?),
                }
            }
            ExprKind::Bin(op, a, b) => self.eval_bin(e, *op, a, b)?,
        };
        Ok(v)
    }

    fn eval_bin(&mut self, e: &Expr, op: BinOp, a: &Expr, b: &Expr) -> Result<Val, FailureSite> {
        let val = match op {
            BinOp::LAnd => Val::Bool(self.eval_bool(a)? && self.eval_bool(b)?),
            BinOp::LOr => Val::Bool(self.eval_bool(a)? || self.eval_bool(b)?),
            BinOp::Eq => {
                match (self.eval(a)?, self.eval(b)?) {
                    (Val::Int(x), Val::Int(y)) => Val::Bool(x == y),
                    (Val::Bool(x), Val::Bool(y)) => Val::Bool(x == y),
                    _ => unreachable!("type-checked"),
                }
            }
            BinOp::Ne => {
                match (self.eval(a)?, self.eval(b)?) {
                    (Val::Int(x), Val::Int(y)) => Val::Bool(x != y),
                    (Val::Bool(x), Val::Bool(y)) => Val::Bool(x != y),
                    _ => unreachable!("type-checked"),
                }
            }
            BinOp::Div | BinOp::Rem => {
                let bits = e.ty.unwrap().bits();
                let av = self.eval_int(a)?;
                // The divisor is evaluated *after* the dividend so its own
                // nested guards fire in source order; a zero divisor is a
                // failure attributed to the divisor expression.
                let bv = match self.eval_int(b) {
                    Err(f) => return Err(f),
                    Ok(0) => {
                        return Err(FailureSite {
                            kind: FailureKind::DivisionByZero,
                            node_id: b.id.get(),
                            line: b.span.line,
                            message: None,
                        })
                    }
                    Ok(v) => v,
                };
                let r = match op {
                    BinOp::Div => av / bv,
                    BinOp::Rem => av % bv,
                    _ => unreachable!(),
                };
                let _ = bits;
                Val::Int(r & e.ty.unwrap().mask())
            }
            _ => {
                let av = self.eval_int(a)?;
                let bv = self.eval_int(b)?;
                // Comparisons produce booleans and carry no width themselves;
                // the width comes from the operands.
                match op {
                    BinOp::Lt => return Ok(Val::Bool(av < bv)),
                    BinOp::Le => return Ok(Val::Bool(av <= bv)),
                    BinOp::Gt => return Ok(Val::Bool(av > bv)),
                    BinOp::Ge => return Ok(Val::Bool(av >= bv)),
                    _ => {}
                }
                let bits = e.ty.unwrap().bits();
                let r = match op {
                    BinOp::Add => sem::add(bits, av, bv),
                    BinOp::Sub => sem::sub(bits, av, bv),
                    BinOp::Mul => sem::mul(bits, av, bv),
                    BinOp::BitAnd => av & bv,
                    BinOp::BitOr => av | bv,
                    BinOp::BitXor => av ^ bv,
                    BinOp::Shl => sem::shl(bits, av, bv),
                    BinOp::Shr => sem::shr(bits, av, bv),
                    _ => unreachable!("handled above"),
                };
                Val::Int(r)
            }
        };
        Ok(val)
    }
}

/// Validate a concrete input assignment against the parameter list.
pub fn validate_input(program: &Program, input: &ConcreteInput) -> Result<(), ConcreteError> {
    for p in &program.params {
        let Some(v) = input.get(&p.name) else {
            return Err(ConcreteError::MissingInput(p.name.clone()));
        };
        if *v & !p.ty.mask() != 0 {
            return Err(ConcreteError::InputOutOfRange {
                name: p.name.clone(),
                ty: p.ty,
                value: *v,
            });
        }
    }
    for k in input.keys() {
        if !program.params.iter().any(|p| &p.name == k) {
            return Err(ConcreteError::ExtraInput(k.clone()));
        }
    }
    Ok(())
}

/// Execute a program on a concrete input. Program-level misuse (missing or
/// out-of-range inputs) is an error; program failures are reported through
/// [`ConcreteResult::failure`].
pub fn run(program: &Program, input: &ConcreteInput) -> Result<ConcreteResult, ConcreteError> {
    validate_input(program, input)?;
    let mut root: HashMap<String, u64> = HashMap::new();
    for p in &program.params {
        root.insert(p.name.clone(), input[&p.name]);
    }
    let mut interp = Interp {
        frames: vec![root],
        steps: Vec::new(),
        _program: program,
    };
    match (|| {
        for s in &program.body {
            interp.stmt(s)?;
        }
        Ok(())
    })() {
        Ok(()) => Ok(ConcreteResult {
            outcome: ConcreteOutcome::Completed,
            failure: None,
            steps: interp.steps,
        }),
        Err(f) => Ok(ConcreteResult {
            outcome: ConcreteOutcome::Failed,
            failure: Some(f),
            steps: interp.steps,
        }),
    }
}
