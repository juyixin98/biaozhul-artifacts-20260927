//! Expression evaluator and specification builder (type checking + name
//! resolution). The evaluator is deliberately independent of the solver:
//! the evidence verifier replays transitions with the same primitives, so a
//! witness can be checked without trusting the search kernel.

use std::collections::HashMap;

use serde::Serialize;

use crate::ast::*;
use crate::error::{BuildError, BuildErrorKind};
use crate::system::{Domain, System, Transition, Value, Var};

/// Kind of runtime evaluation failure.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EvalErrorKind {
    /// Name did not resolve (should normally be caught by type checking).
    UnknownName,
    /// Operand types do not match the operator.
    TypeMismatch,
    /// Integer arithmetic overflowed.
    Overflow,
    /// Division or remainder by zero.
    DivisionByZero,
    /// An assigned value fell outside the target variable's domain.
    OutOfDomain,
    /// The explicit initial state misses a variable.
    MissingVariable,
}

#[derive(Debug, Clone, Serialize)]
pub struct EvalError {
    pub kind: EvalErrorKind,
    pub detail: String,
}

impl EvalError {
    pub fn new(kind: EvalErrorKind, detail: impl Into<String>) -> Self {
        EvalError {
            kind,
            detail: detail.into(),
        }
    }
}

impl std::fmt::Display for EvalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}: {}", self.kind, self.detail)
    }
}

impl std::error::Error for EvalError {}

/// Evaluate an expression against a concrete state (and, for enum variants,
/// the owning system).
pub fn eval(sys: &System, e: &Expr, st: &[Value]) -> Result<Value, EvalError> {
    match e {
        Expr::Bool(b) => Ok(Value::Bool(*b)),
        Expr::Int(i) => Ok(Value::Int(*i)),
        Expr::Name { name, res } => match res {
            Some(i) => Ok(st[*i]),
            None => {
                // unresolved => enum variant
                match sys.variant_value(name) {
                    Some(v) => Ok(Value::Int(v)),
                    None => Err(EvalError::new(
                        EvalErrorKind::UnknownName,
                        format!("name '{name}' is neither a variable nor an enum variant"),
                    )),
                }
            }
        },
        Expr::Unary { op, e } => {
            let v = eval(sys, e, st)?;
            match (op, v) {
                (UnOp::Not, Value::Bool(b)) => Ok(Value::Bool(!b)),
                (UnOp::Neg, Value::Int(i)) => i
                    .checked_neg()
                    .map(Value::Int)
                    .ok_or_else(|| EvalError::new(EvalErrorKind::Overflow, "unary '-' overflowed")),
                (op, v) => Err(EvalError::new(
                    EvalErrorKind::TypeMismatch,
                    format!("{op:?} cannot be applied to {}", v.type_name()),
                )),
            }
        }
        Expr::Binary { op, l, r } => {
            let lv = eval(sys, l, st)?;
            // Short-circuit boolean operators.
            match (op, lv) {
                (BinOp::And, Value::Bool(false)) => return Ok(Value::Bool(false)),
                (BinOp::Or, Value::Bool(true)) => return Ok(Value::Bool(true)),
                _ => {}
            }
            let rv = eval(sys, r, st)?;
            apply_binop(*op, lv, rv)
        }
        Expr::Ite { cond, thn, els } => {
            let c = eval(sys, cond, st)?.as_bool().map_err(|_| {
                EvalError::new(EvalErrorKind::TypeMismatch, "'if' condition is not boolean")
            })?;
            eval(sys, if c { thn } else { els }, st)
        }
    }
}

fn apply_binop(op: BinOp, l: Value, r: Value) -> Result<Value, EvalError> {
    use Value::*;
    let int_arith =
        |f: fn(i64, i64) -> Option<i64>, overflow_msg: &'static str| -> Result<Value, EvalError> {
            match (l, r) {
                (Int(a), Int(b)) => f(a, b)
                    .map(Int)
                    .ok_or_else(|| EvalError::new(EvalErrorKind::Overflow, overflow_msg)),
                _ => Err(EvalError::new(
                    EvalErrorKind::TypeMismatch,
                    format!(
                        "'{}' requires two integers, got {} and {}",
                        op.symbol(),
                        l.type_name(),
                        r.type_name()
                    ),
                )),
            }
        };
    match op {
        BinOp::Add => int_arith(|a, b| a.checked_add(b), "addition overflowed"),
        BinOp::Sub => int_arith(|a, b| a.checked_sub(b), "subtraction overflowed"),
        BinOp::Mul => int_arith(|a, b| a.checked_mul(b), "multiplication overflowed"),
        BinOp::Div => match (l, r) {
            (Int(_), Int(0)) => Err(EvalError::new(
                EvalErrorKind::DivisionByZero,
                "division by zero",
            )),
            (Int(a), Int(b)) => a
                .checked_div(b)
                .map(Int)
                .ok_or_else(|| EvalError::new(EvalErrorKind::Overflow, "division overflowed")),
            _ => Err(EvalError::new(
                EvalErrorKind::TypeMismatch,
                "'/' requires two integers",
            )),
        },
        BinOp::Mod => match (l, r) {
            (Int(_), Int(0)) => Err(EvalError::new(
                EvalErrorKind::DivisionByZero,
                "'mod' by zero",
            )),
            (Int(a), Int(b)) => a
                .checked_rem(b)
                .map(Int)
                .ok_or_else(|| EvalError::new(EvalErrorKind::Overflow, "'mod' overflowed")),
            _ => Err(EvalError::new(
                EvalErrorKind::TypeMismatch,
                "'mod' requires two integers",
            )),
        },
        BinOp::Lt | BinOp::Le | BinOp::Gt | BinOp::Ge => match (l, r) {
            (Int(a), Int(b)) => Ok(Bool(match op {
                BinOp::Lt => a < b,
                BinOp::Le => a <= b,
                BinOp::Gt => a > b,
                _ => a >= b,
            })),
            _ => Err(EvalError::new(
                EvalErrorKind::TypeMismatch,
                format!("'{}' requires two integers", op.symbol()),
            )),
        },
        BinOp::Eq => Ok(Bool(values_equal(l, r))),
        BinOp::Ne => Ok(Bool(!values_equal(l, r))),
        BinOp::And => match (l, r) {
            (Bool(a), Bool(b)) => Ok(Bool(a && b)),
            _ => Err(EvalError::new(
                EvalErrorKind::TypeMismatch,
                "'&&' requires two booleans",
            )),
        },
        BinOp::Or => match (l, r) {
            (Bool(a), Bool(b)) => Ok(Bool(a || b)),
            _ => Err(EvalError::new(
                EvalErrorKind::TypeMismatch,
                "'||' requires two booleans",
            )),
        },
    }
}

/// Equality is strict: comparing an int and a bool is false (the verifier
/// never sees mixed types in well-typed specs, but strictness keeps witness
/// replay from silently accepting tampered evidence).
fn values_equal(l: Value, r: Value) -> bool {
    matches!((l, r), (Value::Bool(a), Value::Bool(b)) if a == b)
        || matches!((l, r), (Value::Int(a), Value::Int(b)) if a == b)
}

/// Infer the static type of an expression while resolving names.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Ty {
    Bool,
    Int,
}

/// Resolve names in a standalone expression (e.g. a property predicate) and
/// check that it is well-typed and boolean against an already-built system.
pub fn check_boolean_predicate(sys: &System, e: &mut Expr) -> Result<(), BuildError> {
    let lookup = |n: &str| -> Result<(Ty, Option<usize>), BuildError> {
        if let Some(i) = sys.var_index(n) {
            let ty = match sys.vars[i].domain {
                Domain::Bool => Ty::Bool,
                _ => Ty::Int,
            };
            Ok((ty, Some(i)))
        } else if sys.variant_value(n).is_some() {
            Ok((Ty::Int, None))
        } else {
            Err(BuildError::new(
                BuildErrorKind::UnknownName,
                format!("unknown name '{n}'"),
            ))
        }
    };
    if check_expr(e, &lookup)? != Ty::Bool {
        return Err(BuildError::new(
            BuildErrorKind::TypeMismatch,
            "property expression must be boolean",
        ));
    }
    resolve_names(e, &lookup)
}

/// Turn a parsed raw specification into a validated, name-resolved
/// [`System`].
pub fn build_system(raw: RawSystem) -> Result<System, BuildError> {
    let RawSystem {
        name,
        vars,
        init_predicate,
        init_state,
        transitions,
        terminals,
    } = raw;

    if vars.is_empty() {
        return Err(BuildError::new(
            BuildErrorKind::InvalidDomain,
            "system declares no state variables",
        ));
    }

    // variable table
    let mut by_name: HashMap<String, usize> = HashMap::new();
    let mut typed_vars: Vec<Var> = Vec::with_capacity(vars.len());
    for (i, (vname, dom)) in vars.iter().enumerate() {
        if by_name.insert(vname.clone(), i).is_some() {
            return Err(BuildError::new(
                BuildErrorKind::DuplicateName,
                format!("duplicate variable '{vname}'"),
            ));
        }
        match dom {
            Domain::IntRange { lo, hi } if hi < lo => {
                return Err(BuildError::new(
                    BuildErrorKind::InvalidDomain,
                    format!("integer range of '{vname}' is empty: [{lo}..{hi}]"),
                ));
            }
            Domain::Enum { variants } if variants.is_empty() => {
                return Err(BuildError::new(
                    BuildErrorKind::InvalidDomain,
                    format!("enum variable '{vname}' has no variants"),
                ));
            }
            Domain::Enum { variants } => {
                let mut seen = HashMap::new();
                for v in variants {
                    if seen.insert(v.clone(), ()).is_some() {
                        return Err(BuildError::new(
                            BuildErrorKind::DuplicateName,
                            format!("enum variable '{vname}' repeats variant '{v}'"),
                        ));
                    }
                }
            }
            _ => {}
        }
        if dom.stride().is_none() {
            return Err(BuildError::new(
                BuildErrorKind::StateSpaceOverflow,
                format!(
                    "domain of '{vname}' is too wide for the explicit-state codec (stride over u64)"
                ),
            ));
        }
        typed_vars.push(Var {
            name: vname.clone(),
            domain: dom.clone(),
            stride: 0,
        });
    }

    // mixed-radix strides: stride[i] = product of sizes to the right
    let mut stride: u128 = 1;
    let mut overflow = false;
    for v in typed_vars.iter_mut().rev() {
        v.stride = u64::try_from(stride).unwrap_or(0);
        if v.stride as u128 != stride {
            overflow = true;
        }
        stride = stride.saturating_mul(v.domain.size().unwrap_or(0));
    }
    if overflow {
        return Err(BuildError::new(
            BuildErrorKind::StateSpaceOverflow,
            "combined state space exceeds the explicit-state codec (mixed-radix product over u64)",
        ));
    }

    // global enum-variant table; ambiguous names are rejected (even when the
    // ambiguity is harmless, so specs stay unambiguous)
    let mut variant_owner: HashMap<String, String> = HashMap::new();
    let mut variant_ordinals: HashMap<String, i64> = HashMap::new();
    for v in &typed_vars {
        if let Domain::Enum { variants } = &v.domain {
            for (ord, varnt) in variants.iter().enumerate() {
                match variant_owner.insert(varnt.clone(), v.name.clone()) {
                    Some(other) if other != v.name => {
                        return Err(BuildError::new(
                            BuildErrorKind::AmbiguousEnumVariant,
                            format!(
                                "enum variant '{varnt}' is declared by both '{other}' and '{}'",
                                v.name
                            ),
                        ));
                    }
                    _ => {}
                }
                variant_ordinals.insert(varnt.clone(), ord as i64);
            }
        }
    }

    let lookup = |n: &str| -> Result<(Ty, Option<usize>), BuildError> {
        if let Some(i) = by_name.get(n) {
            let ty = match typed_vars[*i].domain {
                Domain::Bool => Ty::Bool,
                _ => Ty::Int,
            };
            Ok((ty, Some(*i)))
        } else if variant_owner.contains_key(n) {
            Ok((Ty::Int, None))
        } else {
            Err(BuildError::new(
                BuildErrorKind::UnknownName,
                format!("unknown name '{n}'"),
            ))
        }
    };

    let check = |e: &Expr| -> Result<Ty, BuildError> { check_expr(e, &lookup) };

    // init
    let mut concrete_init: Option<Vec<Value>> = None;
    let init = if let Some(pred) = init_predicate {
        let mut pred = pred;
        if check(&pred)? != Ty::Bool {
            return Err(BuildError::new(
                BuildErrorKind::TypeMismatch,
                "init predicate must be boolean",
            ));
        }
        resolve_names(&mut pred, &lookup)?;
        pred
    } else {
        if init_state.is_empty() {
            return Err(BuildError::new(
                BuildErrorKind::Parse,
                "system has no 'init' section",
            ));
        }
        // every variable must be set exactly once by a well-typed expression
        let mut assigned = vec![false; typed_vars.len()];
        let mut eqs: Vec<Expr> = Vec::new();
        let mut concrete: Vec<Option<Value>> = vec![None; typed_vars.len()];
        for (vn, rhs) in &init_state {
            let Some(&i) = by_name.get(vn) else {
                return Err(BuildError::new(
                    BuildErrorKind::UnknownAssignmentTarget,
                    format!("initial state assigns unknown variable '{vn}'"),
                ));
            };
            if assigned[i] {
                return Err(BuildError::new(
                    BuildErrorKind::DuplicateAssignment,
                    format!("initial state assigns '{vn}' twice"),
                ));
            }
            assigned[i] = true;
            let expected = match typed_vars[i].domain {
                Domain::Bool => Ty::Bool,
                _ => Ty::Int,
            };
            let mut rhs = rhs.clone();
            let got = check(&rhs)?;
            if got != expected {
                return Err(BuildError::new(
                    BuildErrorKind::TypeMismatch,
                    format!("initial value of '{vn}' has the wrong type"),
                ));
            }
            resolve_names(&mut rhs, &lookup)?;
            // An explicit initial value must be a constant expression; this
            // also lets us record the single concrete initial state.
            let val = eval_const(&rhs, &variant_ordinals).map_err(|detail| {
                BuildError::new(
                    BuildErrorKind::TypeMismatch,
                    format!("initial value of '{vn}' must be a literal/enum constant ({detail})"),
                )
            })?;
            if !typed_vars[i].domain.contains(&val) {
                return Err(BuildError::new(
                    BuildErrorKind::InvalidDomain,
                    format!("initial value {val} of '{vn}' is outside its domain"),
                ));
            }
            concrete[i] = Some(val);
            eqs.push(Expr::Binary {
                op: BinOp::Eq,
                l: Box::new(Expr::Name {
                    name: vn.clone(),
                    res: Some(i),
                }),
                r: Box::new(rhs),
            });
        }
        if let Some(miss) = assigned.iter().position(|a| !a) {
            return Err(BuildError::new(
                BuildErrorKind::MissingVariable,
                format!(
                    "initial state does not set variable '{}'",
                    typed_vars[miss].name
                ),
            ));
        }
        concrete_init = Some(concrete.into_iter().map(|v| v.unwrap()).collect());
        eqs.into_iter()
            .rev()
            .fold(Expr::Bool(true), |acc, eq| Expr::Binary {
                op: BinOp::And,
                l: Box::new(eq),
                r: Box::new(acc),
            })
    };

    // transitions
    let mut trans_names: HashMap<String, ()> = HashMap::new();
    let mut out_transitions = Vec::new();
    for t in transitions {
        if trans_names.insert(t.name.clone(), ()).is_some() {
            return Err(BuildError::new(
                BuildErrorKind::DuplicateName,
                format!("duplicate transition '{}'", t.name),
            ));
        }
        let mut guard = t.guard;
        if check(&guard)? != Ty::Bool {
            return Err(BuildError::new(
                BuildErrorKind::TypeMismatch,
                format!("guard of transition '{}' is not boolean", t.name),
            ));
        }
        resolve_names(&mut guard, &lookup)?;

        let mut targets = vec![false; typed_vars.len()];
        let mut out_assign = Vec::new();
        for a in t.assign {
            let Some(&i) = by_name.get(&a.target) else {
                return Err(BuildError::new(
                    BuildErrorKind::UnknownAssignmentTarget,
                    format!(
                        "transition '{}' assigns unknown variable '{}'",
                        t.name, a.target
                    ),
                ));
            };
            if targets[i] {
                return Err(BuildError::new(
                    BuildErrorKind::DuplicateAssignment,
                    format!("transition '{}' assigns '{}' twice", t.name, a.target),
                ));
            }
            targets[i] = true;
            let expected = match typed_vars[i].domain {
                Domain::Bool => Ty::Bool,
                _ => Ty::Int,
            };
            let mut rhs = a.rhs;
            let got = check(&rhs)?;
            if got != expected {
                return Err(BuildError::new(
                    BuildErrorKind::TypeMismatch,
                    format!(
                        "transition '{}' assigns a {} expression to '{}'",
                        t.name,
                        if got == Ty::Bool {
                            "boolean"
                        } else {
                            "integer"
                        },
                        a.target
                    ),
                ));
            }
            resolve_names(&mut rhs, &lookup)?;
            out_assign.push(Assign {
                target: a.target,
                target_index: Some(i),
                rhs,
            });
        }
        out_transitions.push(Transition {
            name: t.name,
            guard,
            assign: out_assign,
        });
    }

    // terminal: OR of all clauses, default false
    let mut terminal = Expr::Bool(false);
    for mut clause in terminals {
        if check(&clause)? != Ty::Bool {
            return Err(BuildError::new(
                BuildErrorKind::TypeMismatch,
                "terminal predicate must be boolean",
            ));
        }
        resolve_names(&mut clause, &lookup)?;
        terminal = Expr::Binary {
            op: BinOp::Or,
            l: Box::new(clause),
            r: Box::new(terminal),
        };
    }

    Ok(System {
        name,
        vars: typed_vars,
        init,
        transitions: out_transitions,
        terminal,
        concrete_init,
    })
}

/// Evaluate a constant initial-value expression: literals, unary minus and
/// enum-variant names (resolved through `variants`).
fn eval_const(e: &Expr, variants: &HashMap<String, i64>) -> Result<Value, String> {
    match e {
        Expr::Bool(b) => Ok(Value::Bool(*b)),
        Expr::Int(i) => Ok(Value::Int(*i)),
        Expr::Name { name, res: None } => variants
            .get(name)
            .copied()
            .map(Value::Int)
            .ok_or_else(|| format!("{name} is not a constant")),
        Expr::Unary { op: UnOp::Neg, e } => match eval_const(e, variants)? {
            Value::Int(i) => Ok(Value::Int(i.checked_neg().ok_or("negation overflow")?)),
            _ => Err("unary '-' on non-integer".into()),
        },
        _ => Err("expression is not a compile-time constant".into()),
    }
}

fn check_expr<F>(e: &Expr, lookup: &F) -> Result<Ty, BuildError>
where
    F: Fn(&str) -> Result<(Ty, Option<usize>), BuildError>,
{
    match e {
        Expr::Bool(_) => Ok(Ty::Bool),
        Expr::Int(_) => Ok(Ty::Int),
        Expr::Name { name, .. } => Ok(lookup(name)?.0),
        Expr::Unary { op, e } => {
            let t = check_expr(e, lookup)?;
            match (op, t) {
                (UnOp::Not, Ty::Bool) => Ok(Ty::Bool),
                (UnOp::Neg, Ty::Int) => Ok(Ty::Int),
                _ => Err(BuildError::new(
                    BuildErrorKind::TypeMismatch,
                    format!("{op:?} applied to expression of type {t:?}"),
                )),
            }
        }
        Expr::Binary { op, l, r } => {
            let lt = check_expr(l, lookup)?;
            let rt = check_expr(r, lookup)?;
            match op {
                BinOp::And | BinOp::Or => {
                    if lt == Ty::Bool && rt == Ty::Bool {
                        Ok(Ty::Bool)
                    } else {
                        Err(BuildError::new(
                            BuildErrorKind::TypeMismatch,
                            format!("'{:?}' requires booleans", op.symbol()),
                        ))
                    }
                }
                BinOp::Add | BinOp::Sub | BinOp::Mul | BinOp::Div | BinOp::Mod => {
                    if lt == Ty::Int && rt == Ty::Int {
                        Ok(Ty::Int)
                    } else {
                        Err(BuildError::new(
                            BuildErrorKind::TypeMismatch,
                            format!("'{}' requires integers", op.symbol()),
                        ))
                    }
                }
                BinOp::Lt | BinOp::Le | BinOp::Gt | BinOp::Ge => {
                    if lt == Ty::Int && rt == Ty::Int {
                        Ok(Ty::Bool)
                    } else {
                        Err(BuildError::new(
                            BuildErrorKind::TypeMismatch,
                            format!("'{}' requires integers", op.symbol()),
                        ))
                    }
                }
                BinOp::Eq | BinOp::Ne => {
                    if lt == rt {
                        Ok(Ty::Bool)
                    } else {
                        Err(BuildError::new(
                            BuildErrorKind::TypeMismatch,
                            format!(
                                "'{}' compares mismatched types {lt:?} and {rt:?}",
                                op.symbol()
                            ),
                        ))
                    }
                }
            }
        }
        Expr::Ite { cond, thn, els } => {
            if check_expr(cond, lookup)? != Ty::Bool {
                return Err(BuildError::new(
                    BuildErrorKind::TypeMismatch,
                    "'if' condition must be boolean",
                ));
            }
            let a = check_expr(thn, lookup)?;
            let b = check_expr(els, lookup)?;
            if a == b {
                Ok(a)
            } else {
                Err(BuildError::new(
                    BuildErrorKind::TypeMismatch,
                    "'if' branches have different types",
                ))
            }
        }
    }
}

/// Fill in variable resolution indices; enum variants stay `res == None`.
fn resolve_names<F>(e: &mut Expr, lookup: &F) -> Result<(), BuildError>
where
    F: Fn(&str) -> Result<(Ty, Option<usize>), BuildError>,
{
    match e {
        Expr::Bool(_) | Expr::Int(_) => {}
        Expr::Name { name, res } => {
            *res = lookup(name)?.1;
        }
        Expr::Unary { e, .. } => resolve_names(e, lookup)?,
        Expr::Binary { l, r, .. } => {
            resolve_names(l, lookup)?;
            resolve_names(r, lookup)?;
        }
        Expr::Ite { cond, thn, els } => {
            resolve_names(cond, lookup)?;
            resolve_names(thn, lookup)?;
            resolve_names(els, lookup)?;
        }
    }
    Ok(())
}
