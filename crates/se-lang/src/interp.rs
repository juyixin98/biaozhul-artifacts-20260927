//! Independent concrete interpreter for the input language.
//!
//! This interpreter is written directly over the AST with its own recursion and
//! store; it does not consult the symbolic engine or the SMT solver.  It serves two
//! masters:
//!
//! 1. **Evidence replay** — a counterexample produced by the engine is fed back through
//!    [`run`]; the reported failure must reproduce at the same statement.
//! 2. **Exhaustive oracle** — [`crate::interp`] is invoked for every assignment of a
//!    small input domain to compute the ground-truth verdict the engine is compared
//!    against in independent tests.
//!
//! Failures are classified explicitly (see [`FailureKind`]); nothing is collapsed into
//! a generic success.

use std::collections::BTreeMap;

use crate::ast::{Expr, OverflowMode, Program, Stmt, UnOp, Width};
use crate::bits;

/// Classification of a concrete execution failure. The string representation is part
/// of the public JSON contract (see `FailureKind::as_str`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailureKind {
    /// `assert cond` with `cond == 0`.
    Assertion,
    /// Division or remainder by zero.
    DivByZero,
    /// Arithmetic overflow with `overflow = "trap"` (or signed `INT_MIN / -1`).
    Overflow,
    /// The concrete step budget was exhausted (guarantees the interpreter terminates
    /// even on programs the bounded symbolic analysis marks unknown).
    StepLimit,
}

impl FailureKind {
    pub fn as_str(self) -> &'static str {
        match self {
            FailureKind::Assertion => "assertion",
            FailureKind::DivByZero => "div_by_zero",
            FailureKind::Overflow => "overflow",
            FailureKind::StepLimit => "step_limit",
        }
    }

    pub fn parse(s: &str) -> Option<FailureKind> {
        Some(match s {
            "assertion" => FailureKind::Assertion,
            "div_by_zero" => FailureKind::DivByZero,
            "overflow" => FailureKind::Overflow,
            "step_limit" => FailureKind::StepLimit,
            _ => return None,
        })
    }
}

impl serde::Serialize for FailureKind {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> {
        s.serialize_str(self.as_str())
    }
}

impl<'de> serde::Deserialize<'de> for FailureKind {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        let txt = <String as serde::Deserialize>::deserialize(d)?;
        FailureKind::parse(&txt)
            .ok_or_else(|| serde::de::Error::custom(format!("unknown failure kind '{txt}'")))
    }
}

/// A located failure. `stmt_id` is the id of the statement on which it surfaced;
/// `op` names the failing operation when the cause is inside an expression.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub kind: FailureKind,
    pub stmt_id: usize,
    pub op: Option<&'static str>,
}

impl Failure {
    fn at(kind: FailureKind, stmt_id: usize, op: Option<&'static str>) -> Self {
        Failure {
            kind,
            stmt_id,
            op,
        }
    }
}

/// Terminal flow outcome of one concrete execution.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FlowOutcome {
    /// The program body finished without violating anything.
    Completed,
    /// A safety failure occurred.
    Failed(Failure),
    /// An `assume` condition evaluated to 0; the assignment is infeasible.
    InfeasibleAssume,
}

#[derive(Clone, Debug)]
pub struct RunOpts {
    /// Maximum number of interpreted steps (statements + while-condition checks).
    pub step_limit: u64,
    /// Record executed statement ids into `RunResult::trace`.
    pub record_trace: bool,
}

impl Default for RunOpts {
    fn default() -> Self {
        RunOpts {
            step_limit: 200_000,
            record_trace: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct RunResult {
    pub outcome: FlowOutcome,
    /// Final store (inputs and declared vars), sorted for deterministic output.
    pub final_store: BTreeMap<String, u64>,
    pub steps: u64,
    /// Executed statement ids in order, when `record_trace` is enabled.
    pub trace: Vec<usize>,
}

type EvalError = (FailureKind, &'static str);

/// Concrete expression evaluator. Returns either a masked w-bit value or a hard
/// failure (division by zero is always fatal; overflow is fatal only in trap mode).
fn eval_expr(
    e: &Expr,
    env: &mut BTreeMap<String, u64>,
    w: Width,
    mode: OverflowMode,
) -> Result<u64, EvalError> {
    match e {
        Expr::Int(n) => Ok(bits::mask(*n, w)),
        Expr::Var(name) => Ok(*env.get(name).expect("lowering guarantees name resolution")),
        Expr::Un { op, arg } => {
            let a = eval_expr(arg, env, w, mode)?;
            let r = bits::eval_un(*op, a, w, mode);
            post_check(r, match op {
                UnOp::Neg => "neg",
                UnOp::Not => "not",
            }, mode)
        }
        Expr::Bin { op, lhs, rhs } => {
            let a = eval_expr(lhs, env, w, mode)?;
            let b = eval_expr(rhs, env, w, mode)?;
            let r = bits::eval_bin(*op, a, b, w, mode);
            post_check(r, op.as_str(), mode)
        }
        Expr::Ite { cond, then, els } => {
            let c = eval_expr(cond, env, w, mode)?;
            if bits::is_true(c) {
                eval_expr(then, env, w, mode)
            } else {
                eval_expr(els, env, w, mode)
            }
        }
    }
}

fn post_check(
    r: bits::ArithOutcome,
    op: &'static str,
    mode: OverflowMode,
) -> Result<u64, EvalError> {
    if r.div_by_zero {
        Err((FailureKind::DivByZero, op))
    } else if r.overflow && mode == OverflowMode::Trap {
        // In trap mode the bit layer returns overflow=true as a failure; in wrap mode
        // the same flag merely describes that wrapping occurred and the wrapped value
        // is the defined result.
        Err((FailureKind::Overflow, op))
    } else {
        Ok(r.value)
    }
}

struct Ctx<'a> {
    program: &'a Program,
    env: BTreeMap<String, u64>,
    steps: u64,
    trace: Vec<usize>,
    opts: RunOpts,
}

impl<'a> Ctx<'a> {
    fn tick(&mut self) -> Result<(), Failure> {
        self.steps += 1;
        if self.steps > self.opts.step_limit {
            // Location is filled in by the caller; StepLimit at id 0 is overwritten.
            Err(Failure::at(FailureKind::StepLimit, usize::MAX, None))
        } else {
            Ok(())
        }
    }

    fn exec_block(&mut self, blk: &[Stmt]) -> FlowOutcome {
        for stmt in blk {
            let out = self.exec_stmt(stmt);
            if !matches!(out, FlowOutcome::Completed) {
                return out;
            }
        }
        FlowOutcome::Completed
    }

    fn exec_stmt(&mut self, stmt: &Stmt) -> FlowOutcome {
        let id = stmt.id();
        if self.opts.record_trace {
            self.trace.push(id);
        }
        if self.tick().is_err() {
            return FlowOutcome::Failed(Failure::at(FailureKind::StepLimit, id, None));
        }
        let w = self.program.width;
        let mode = self.program.overflow;

        match stmt {
            Stmt::Assign { target, expr, .. } => {
                match eval_expr(expr, &mut self.env, w, mode) {
                    Ok(v) => {
                        self.env.insert(target.clone(), bits::mask(v, w));
                        FlowOutcome::Completed
                    }
                    Err((kind, op)) => FlowOutcome::Failed(Failure::at(kind, id, Some(op))),
                }
            }
            Stmt::If {
                cond,
                then_blk,
                else_blk,
                ..
            } => match eval_expr(cond, &mut self.env, w, mode) {
                Ok(v) => {
                    if bits::is_true(v) {
                        self.exec_block(then_blk)
                    } else {
                        self.exec_block(else_blk)
                    }
                }
                Err((kind, op)) => FlowOutcome::Failed(Failure::at(kind, id, Some(op))),
            },
            Stmt::While { cond, body, .. } => {
                loop {
                    // Each condition check counts as a step too.
                    if self.tick().is_err() {
                        return FlowOutcome::Failed(Failure::at(
                            FailureKind::StepLimit,
                            id,
                            None,
                        ));
                    }
                    match eval_expr(cond, &mut self.env, w, mode) {
                        Ok(v) => {
                            if !bits::is_true(v) {
                                return FlowOutcome::Completed;
                            }
                        }
                        Err((kind, op)) => {
                            return FlowOutcome::Failed(Failure::at(kind, id, Some(op)))
                        }
                    }
                    let out = self.exec_block(body);
                    match out {
                        FlowOutcome::Completed => continue,
                        other => return other,
                    }
                }
            }
            Stmt::Assume { cond, .. } => {
                match eval_expr(cond, &mut self.env, w, mode) {
                    Ok(v) => {
                        if bits::is_true(v) {
                            FlowOutcome::Completed
                        } else {
                            FlowOutcome::InfeasibleAssume
                        }
                    }
                    Err((kind, op)) => FlowOutcome::Failed(Failure::at(kind, id, Some(op))),
                }
            }
            Stmt::Assert { cond, .. } => match eval_expr(cond, &mut self.env, w, mode) {
                Ok(v) => {
                    if bits::is_true(v) {
                        FlowOutcome::Completed
                    } else {
                        FlowOutcome::Failed(Failure::at(FailureKind::Assertion, id, None))
                    }
                }
                Err((kind, op)) => FlowOutcome::Failed(Failure::at(kind, id, Some(op))),
            },
        }
    }
}

/// Execute `program` on one concrete `inputs` assignment.
///
/// `inputs` maps input name → unsigned w-bit value; values are masked to the program
/// width and clamped into declared domains by callers when needed.
pub fn run(program: &Program, inputs: &BTreeMap<String, u64>, opts: RunOpts) -> RunResult {
    let mut env: BTreeMap<String, u64> = BTreeMap::new();
    for var in &program.vars {
        env.insert(var.name.clone(), bits::mask(var.init, program.width));
    }
    for input in &program.inputs {
        let raw = inputs.get(&input.name).copied().unwrap_or(input.low);
        env.insert(input.name.clone(), bits::mask(raw, program.width));
    }

    let mut ctx = Ctx {
        program,
        env,
        steps: 0,
        trace: Vec::new(),
        opts,
    };
    let outcome = ctx.exec_block(&program.body);
    RunResult {
        outcome,
        final_store: ctx.env,
        steps: ctx.steps,
        trace: ctx.trace,
    }
}

/// Enumerate every input assignment inside the declared domains, in lexicographic order
/// by [`Program::inputs`] order. Stops after `cap` assignments (returning `false`).
///
/// Returns `true` if the whole domain fit under `cap`, `false` if enumeration was
/// truncated. Each invocation of `f` receives `(assignment_index, assignment)`.
pub fn enumerate_inputs<F: FnMut(u64, &BTreeMap<String, u64>)>(
    program: &Program,
    cap: u64,
    mut f: F,
) -> bool {
    let names: Vec<String> = program.inputs.iter().map(|i| i.name.clone()).collect();
    let bounds: Vec<(u64, u64)> = program
        .inputs
        .iter()
        .map(|i| (i.low, i.high))
        .collect();

    let mut current: Vec<u64> = bounds.iter().map(|(lo, _)| *lo).collect();
    let mut index = 0u64;

    if bounds.iter().any(|(lo, hi)| lo > hi) {
        return true; // empty domain: exhaustively "nothing to enumerate"
    }

    loop {
        if index >= cap {
            return false;
        }
        let assignment: BTreeMap<String, u64> = names
            .iter()
            .zip(current.iter())
            .map(|(n, v)| (n.clone(), *v))
            .collect();
        f(index, &assignment);
        index += 1;

        // Increment as a mixed-radix counter (last input varies fastest).
        let mut carry = true;
        for k in (0..bounds.len()).rev() {
            if !carry {
                break;
            }
            let (lo, hi) = bounds[k];
            if current[k] < hi {
                current[k] += 1;
                carry = false;
            } else {
                current[k] = lo;
                carry = true;
            }
        }
        if carry {
            return true; // wrapped past the most significant digit: done
        }
    }
}

/// Domain size (number of input assignments), if it fits in u64.
pub fn domain_size(program: &Program) -> u128 {
    program
        .inputs
        .iter()
        .map(|i| (i.high as u128) - (i.low as u128) + 1)
        .product()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dto::ProgramDto;

    fn run_json(src: &str, x: u64) -> FlowOutcome {
        let dto: ProgramDto = serde_json::from_str(src).unwrap();
        let (p, _) = dto.lower().unwrap();
        let mut inputs = BTreeMap::new();
        inputs.insert("x".to_string(), x);
        run(&p, &inputs, RunOpts::default()).outcome
    }

    #[test]
    fn assert_fires_concretely() {
        let prog = r#"{"width":8,"inputs":[{"name":"x","low":0,"high":255}],
            "body":[{"stmt":"assert","cond":{"expr":"ule","lhs":{"expr":"var","name":"x"},"rhs":{"expr":"int","value":10}}}]}"#;
        assert!(matches!(
            run_json(prog, 20),
            FlowOutcome::Failed(Failure {
                kind: FailureKind::Assertion,
                ..
            })
        ));
        assert_eq!(run_json(prog, 10), FlowOutcome::Completed);
    }

    #[test]
    fn while_loop_runs_concretely() {
        let dto: ProgramDto = serde_json::from_str(
            r#"{"width":8,"inputs":[{"name":"n","low":0,"high":20}],
            "vars":[{"name":"i","value":0}],
            "body":[
              {"stmt":"while","cond":{"expr":"ult","lhs":{"expr":"var","name":"i"},"rhs":{"expr":"var","name":"n"}},
               "body":[{"stmt":"assign","target":"i","expr":{"expr":"add","lhs":{"expr":"var","name":"i"},"rhs":{"expr":"int","value":1}}}]},
              {"stmt":"assert","cond":{"expr":"eq","lhs":{"expr":"var","name":"i"},"rhs":{"expr":"var","name":"n"}}}
            ]}"#,
        )
        .unwrap();
        let (p, _) = dto.lower().unwrap();
        let mut inputs = BTreeMap::new();
        inputs.insert("n".to_string(), 5u64);
        let res = run(&p, &inputs, RunOpts::default());
        assert_eq!(res.outcome, FlowOutcome::Completed);
        assert_eq!(res.final_store["i"], 5);
    }

    #[test]
    fn enumerator_counts_domain() {
        let dto: ProgramDto = serde_json::from_str(
            r#"{"width":8,"inputs":[{"name":"x","low":0,"high":1},{"name":"y","low":0,"high":2}],"body":[]}"#,
        )
        .unwrap();
        let (p, _) = dto.lower().unwrap();
        let mut count = 0;
        assert!(enumerate_inputs(&p, 1000, |_, _| count += 1));
        assert_eq!(count, 6);
        assert_eq!(domain_size(&p), 6);
    }
}
