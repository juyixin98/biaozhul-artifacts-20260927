//! Compilation: name/type resolution, domain analysis and expression
//! evaluation over concrete states.

use crate::error::{EvalError, LangError};
use crate::model::*;
use serde::{Deserialize, Serialize};

/// The two static types of the language.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Type {
    Int,
    Bool,
}

impl Type {
    fn of(value: &Value) -> Self {
        match value {
            Value::Int(_) => Type::Int,
            Value::Bool(_) => Type::Bool,
        }
    }
}

impl Domain {
    pub fn ty(&self) -> Type {
        match self {
            Domain::IntRange { .. } => Type::Int,
            Domain::Bool => Type::Bool,
        }
    }

    pub fn size(&self) -> u128 {
        match self {
            // Compute in signed 128-bit so negative lower bounds do not
            // underflow when cast to u128.
            Domain::IntRange { lo, hi } => (*hi as i128 - *lo as i128 + 1) as u128,
            Domain::Bool => 2,
        }
    }

    pub fn contains(&self, value: &Value) -> bool {
        match (self, value) {
            (Domain::IntRange { lo, hi }, Value::Int(v)) => v >= lo && v <= hi,
            (Domain::Bool, Value::Bool(_)) => true,
            _ => false,
        }
    }

    pub fn digit_for(&self, value: &Value) -> Option<u128> {
        match (self, value) {
            (Domain::IntRange { lo, .. }, Value::Int(v)) => {
                Some((*v as i128 - *lo as i128) as u128)
            }
            (Domain::Bool, Value::Bool(false)) => Some(0),
            (Domain::Bool, Value::Bool(true)) => Some(1),
            _ => None,
        }
    }
}

/// Resolved expression: variable references become positional indices into
/// the variable vector, so evaluation needs no name lookup.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RExpr {
    Const(Value),
    Var(usize),
    Not(Box<RExpr>),
    Neg(Box<RExpr>),
    And(Box<RExpr>, Box<RExpr>),
    Or(Box<RExpr>, Box<RExpr>),
    Implies(Box<RExpr>, Box<RExpr>),
    Add(Box<RExpr>, Box<RExpr>),
    Sub(Box<RExpr>, Box<RExpr>),
    Mul(Box<RExpr>, Box<RExpr>),
    Eq(Box<RExpr>, Box<RExpr>),
    Ne(Box<RExpr>, Box<RExpr>),
    Lt(Box<RExpr>, Box<RExpr>),
    Le(Box<RExpr>, Box<RExpr>),
    Gt(Box<RExpr>, Box<RExpr>),
    Ge(Box<RExpr>, Box<RExpr>),
}

fn type_err(msg: impl Into<String>) -> LangError {
    LangError::new("TYPE_MISMATCH", msg)
}

impl RExpr {
    fn resolve(expr: &Expr, vars: &[Variable]) -> Result<(RExpr, Type), LangError> {
        match expr {
            Expr::Const { value } => Ok((RExpr::Const(value.clone()), Type::of(value))),
            Expr::Var { name } => match vars.iter().position(|v| v.name == *name) {
                Some(idx) => Ok((RExpr::Var(idx), vars[idx].domain.ty())),
                None => Err(LangError::new(
                    "UNKNOWN_VARIABLE",
                    format!("unknown variable `{name}`"),
                )),
            },
            Expr::Not { expr } => {
                let (e, t) = RExpr::resolve(expr, vars)?;
                if t != Type::Bool {
                    return Err(type_err("`not` requires a boolean operand"));
                }
                Ok((RExpr::Not(Box::new(e)), Type::Bool))
            }
            Expr::Neg { expr } => {
                let (e, t) = RExpr::resolve(expr, vars)?;
                if t != Type::Int {
                    return Err(type_err("unary `-` requires an integer operand"));
                }
                Ok((RExpr::Neg(Box::new(e)), Type::Int))
            }
            Expr::And { left, right }
            | Expr::Or { left, right }
            | Expr::Implies { left, right } => {
                let (l, lt) = RExpr::resolve(left, vars)?;
                let (r, rt) = RExpr::resolve(right, vars)?;
                if lt != Type::Bool || rt != Type::Bool {
                    return Err(type_err(format!(
                        "`{}` requires boolean operands, got {} and {}",
                        op_name(expr),
                        lt_name(lt),
                        lt_name(rt)
                    )));
                }
                let node = match expr {
                    Expr::And { .. } => RExpr::And(Box::new(l), Box::new(r)),
                    Expr::Or { .. } => RExpr::Or(Box::new(l), Box::new(r)),
                    Expr::Implies { .. } => RExpr::Implies(Box::new(l), Box::new(r)),
                    _ => unreachable!(),
                };
                Ok((node, Type::Bool))
            }
            Expr::Add { left, right }
            | Expr::Sub { left, right }
            | Expr::Mul { left, right } => {
                let (l, lt) = RExpr::resolve(left, vars)?;
                let (r, rt) = RExpr::resolve(right, vars)?;
                if lt != Type::Int || rt != Type::Int {
                    return Err(type_err(format!(
                        "`{}` requires integer operands, got {} and {}",
                        op_name(expr),
                        lt_name(lt),
                        lt_name(rt)
                    )));
                }
                let node = match expr {
                    Expr::Add { .. } => RExpr::Add(Box::new(l), Box::new(r)),
                    Expr::Sub { .. } => RExpr::Sub(Box::new(l), Box::new(r)),
                    Expr::Mul { .. } => RExpr::Mul(Box::new(l), Box::new(r)),
                    _ => unreachable!(),
                };
                Ok((node, Type::Int))
            }
            Expr::Eq { left, right } | Expr::Ne { left, right } => {
                let (l, lt) = RExpr::resolve(left, vars)?;
                let (r, rt) = RExpr::resolve(right, vars)?;
                if lt != rt {
                    return Err(type_err(format!(
                        "`{}` compares values of different types: {} and {}",
                        op_name(expr),
                        lt_name(lt),
                        lt_name(rt)
                    )));
                }
                let node = match expr {
                    Expr::Eq { .. } => RExpr::Eq(Box::new(l), Box::new(r)),
                    _ => RExpr::Ne(Box::new(l), Box::new(r)),
                };
                Ok((node, Type::Bool))
            }
            Expr::Lt { left, right }
            | Expr::Le { left, right }
            | Expr::Gt { left, right }
            | Expr::Ge { left, right } => {
                let (l, lt) = RExpr::resolve(left, vars)?;
                let (r, rt) = RExpr::resolve(right, vars)?;
                if lt != Type::Int || rt != Type::Int {
                    return Err(type_err(format!(
                        "`{}` requires integer operands, got {} and {}",
                        op_name(expr),
                        lt_name(lt),
                        lt_name(rt)
                    )));
                }
                let node = match expr {
                    Expr::Lt { .. } => RExpr::Lt(Box::new(l), Box::new(r)),
                    Expr::Le { .. } => RExpr::Le(Box::new(l), Box::new(r)),
                    Expr::Gt { .. } => RExpr::Gt(Box::new(l), Box::new(r)),
                    _ => RExpr::Ge(Box::new(l), Box::new(r)),
                };
                Ok((node, Type::Bool))
            }
        }
    }

    /// Evaluate over a fully assigned state. All reads use this single
    /// pre-state: simultaneous updates are implemented by the caller
    /// evaluating every RHS before any assignment.
    pub fn eval(&self, state: &[Value]) -> Result<Value, EvalError> {
        match self {
            RExpr::Const(v) => Ok(v.clone()),
            RExpr::Var(i) => Ok(state[*i].clone()),
            RExpr::Not(e) => match e.eval(state)? {
                Value::Bool(b) => Ok(Value::Bool(!b)),
                other => Err(EvalError::new(
                    "TYPE_MISMATCH",
                    format!("`not` on {}", other.type_name()),
                )),
            },
            RExpr::Neg(e) => match e.eval(state)? {
                Value::Int(v) => v.checked_neg().ok_or_else(|| {
                    EvalError::new("INT_OVERFLOW", "negation overflows i64")
                }).map(Value::Int),
                other => Err(EvalError::new(
                    "TYPE_MISMATCH",
                    format!("unary `-` on {}", other.type_name()),
                )),
            },
            RExpr::And(l, r) => {
                let lb = as_bool(l.eval(state)?)?;
                let rb = as_bool(r.eval(state)?)?;
                Ok(Value::Bool(lb && rb))
            }
            RExpr::Or(l, r) => {
                let lb = as_bool(l.eval(state)?)?;
                let rb = as_bool(r.eval(state)?)?;
                Ok(Value::Bool(lb || rb))
            }
            RExpr::Implies(l, r) => {
                let lb = as_bool(l.eval(state)?)?;
                let rb = as_bool(r.eval(state)?)?;
                Ok(Value::Bool(!lb || rb))
            }
            RExpr::Add(l, r) => arith(&l.eval(state)?, &r.eval(state)?, i64::checked_add),
            RExpr::Sub(l, r) => arith(&l.eval(state)?, &r.eval(state)?, i64::checked_sub),
            RExpr::Mul(l, r) => arith(&l.eval(state)?, &r.eval(state)?, i64::checked_mul),
            RExpr::Eq(l, r) => Ok(Value::Bool(values_equal(&l.eval(state)?, &r.eval(state)?)?)),
            RExpr::Ne(l, r) => Ok(Value::Bool(!values_equal(&l.eval(state)?, &r.eval(state)?)?)),
            RExpr::Lt(l, r) => cmp(&l.eval(state)?, &r.eval(state)?, |o| o == std::cmp::Ordering::Less),
            RExpr::Le(l, r) => cmp(&l.eval(state)?, &r.eval(state)?, |o| o != std::cmp::Ordering::Greater),
            RExpr::Gt(l, r) => cmp(&l.eval(state)?, &r.eval(state)?, |o| o == std::cmp::Ordering::Greater),
            RExpr::Ge(l, r) => cmp(&l.eval(state)?, &r.eval(state)?, |o| o != std::cmp::Ordering::Less),
        }
    }

    pub fn eval_bool(&self, state: &[Value]) -> Result<bool, EvalError> {
        as_bool(self.eval(state)?)
    }
}

fn op_name(expr: &Expr) -> &'static str {
    match expr {
        Expr::Not { .. } => "not",
        Expr::Neg { .. } => "neg",
        Expr::And { .. } => "and",
        Expr::Or { .. } => "or",
        Expr::Implies { .. } => "implies",
        Expr::Add { .. } => "add",
        Expr::Sub { .. } => "sub",
        Expr::Mul { .. } => "mul",
        Expr::Eq { .. } => "eq",
        Expr::Ne { .. } => "ne",
        Expr::Lt { .. } => "lt",
        Expr::Le { .. } => "le",
        Expr::Gt { .. } => "gt",
        Expr::Ge { .. } => "ge",
        Expr::Const { .. } | Expr::Var { .. } => "?",
    }
}

fn lt_name(t: Type) -> &'static str {
    match t {
        Type::Int => "int",
        Type::Bool => "bool",
    }
}

fn as_bool(value: Value) -> Result<bool, EvalError> {
    match value {
        Value::Bool(b) => Ok(b),
        other => Err(EvalError::new(
            "TYPE_MISMATCH",
            format!("expected bool, got {}", other.type_name()),
        )),
    }
}

fn values_equal(a: &Value, b: &Value) -> Result<bool, EvalError> {
    match (a, b) {
        (Value::Int(x), Value::Int(y)) => Ok(x == y),
        (Value::Bool(x), Value::Bool(y)) => Ok(x == y),
        _ => Err(EvalError::new(
            "TYPE_MISMATCH",
            format!("cannot compare {} and {}", a.type_name(), b.type_name()),
        )),
    }
}

fn arith(
    a: &Value,
    b: &Value,
    f: fn(i64, i64) -> Option<i64>,
) -> Result<Value, EvalError> {
    match (a, b) {
        (Value::Int(x), Value::Int(y)) => f(*x, *y)
            .map(Value::Int)
            .ok_or_else(|| EvalError::new("INT_OVERFLOW", "integer arithmetic overflowed i64")),
        _ => Err(EvalError::new(
            "TYPE_MISMATCH",
            format!("arithmetic on {} and {}", a.type_name(), b.type_name()),
        )),
    }
}

fn cmp(a: &Value, b: &Value, pred: fn(std::cmp::Ordering) -> bool) -> Result<Value, EvalError> {
    match (a, b) {
        (Value::Int(x), Value::Int(y)) => Ok(Value::Bool(pred(x.cmp(y)))),
        _ => Err(EvalError::new(
            "TYPE_MISMATCH",
            format!("ordering {} and {}", a.type_name(), b.type_name()),
        )),
    }
}

/// A compiled assignment: target variable index plus its (pre-state) RHS.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RUpdate {
    pub target: usize,
    pub target_name: String,
    pub value: RExpr,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RTransition {
    pub name: String,
    pub guard: RExpr,
    pub updates: Vec<RUpdate>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RProperty {
    pub name: String,
    pub kind: PropertyKind,
    pub predicate: RExpr,
}

/// Specification after name/type resolution. This is the object the kernel
/// and the independent evidence verifier both consume.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CompiledSpec {
    pub name: String,
    pub variables: Vec<Variable>,
    pub initial: RExpr,
    pub transitions: Vec<RTransition>,
    pub terminal: Option<RExpr>,
    pub properties: Vec<RProperty>,
    digits: Vec<u128>,
    strides: Vec<u128>,
}

pub fn default_true() -> Expr {
    Expr::Const {
        value: Value::Bool(true),
    }
}

impl CompiledSpec {
    /// Resolve and validate a [`Spec`].
    pub fn compile(spec: &Spec) -> Result<Self, LangError> {
        if spec.name.trim().is_empty() {
            return Err(LangError::new(
                "INVALID_SPEC",
                "specification name must be non-empty",
            ));
        }
        if spec.variables.is_empty() {
            return Err(LangError::new(
                "INVALID_SPEC",
                "at least one state variable is required",
            ));
        }
        for var in &spec.variables {
            if var.name.trim().is_empty() {
                return Err(LangError::new("INVALID_SPEC", "variable name must be non-empty"));
            }
            if let Domain::IntRange { lo, hi } = &var.domain {
                if lo > hi {
                    return Err(LangError::new(
                        "INVALID_DOMAIN",
                        format!("variable `{}` has empty int range [{}, {}]", var.name, lo, hi),
                    ));
                }
            }
        }
        for (i, v) in spec.variables.iter().enumerate() {
            if spec.variables[..i].iter().any(|o| o.name == v.name) {
                return Err(LangError::new(
                    "DUPLICATE_NAME",
                    format!("duplicate variable name `{}`", v.name),
                ));
            }
        }
        if spec.transitions.is_empty() {
            return Err(LangError::new(
                "INVALID_SPEC",
                "at least one transition must be declared",
            ));
        }
        for (i, t) in spec.transitions.iter().enumerate() {
            if t.name.trim().is_empty() {
                return Err(LangError::new("INVALID_SPEC", "transition name must be non-empty"));
            }
            if spec.transitions[..i].iter().any(|o| o.name == t.name) {
                return Err(LangError::new(
                    "DUPLICATE_NAME",
                    format!("duplicate transition name `{}`", t.name),
                ));
            }
        }
        for (i, p) in spec.properties.iter().enumerate() {
            if p.name.trim().is_empty() {
                return Err(LangError::new("INVALID_SPEC", "property name must be non-empty"));
            }
            if spec.properties[..i].iter().any(|o| o.name == p.name) {
                return Err(LangError::new(
                    "DUPLICATE_NAME",
                    format!("duplicate property name `{}`", p.name),
                ));
            }
        }

        let vars = &spec.variables;
        let resolve_pred = |e: &Expr, what: String| -> Result<RExpr, LangError> {
            let (r, t) = RExpr::resolve(e, vars)?;
            if t != Type::Bool {
                return Err(type_err(format!("{what} must be a boolean expression")));
            }
            Ok(r)
        };

        let initial = resolve_pred(&spec.initial, "initial predicate".to_string())?;
        let terminal = match &spec.terminal {
            Some(e) => Some(resolve_pred(e, "terminal predicate".to_string())?),
            None => None,
        };

        let mut transitions = Vec::with_capacity(spec.transitions.len());
        for t in &spec.transitions {
            let guard = resolve_pred(
                &t.guard,
                format!("guard of transition `{}`", t.name),
            )?;
            let mut updates = Vec::with_capacity(t.updates.len());
            for u in &t.updates {
                let idx = vars
                    .iter()
                    .position(|v| v.name == u.var)
                    .ok_or_else(|| {
                        LangError::new(
                            "UNKNOWN_VARIABLE",
                            format!(
                                "transition `{}` assigns unknown variable `{}`",
                                t.name, u.var
                            ),
                        )
                    })?;
                let (rhs, rhs_ty) = RExpr::resolve(&u.value, vars)?;
                if rhs_ty != vars[idx].domain.ty() {
                    return Err(type_err(format!(
                        "transition `{}` assigns {} to variable `{}` of type {}",
                        t.name,
                        lt_name(rhs_ty),
                        vars[idx].name,
                        lt_name(vars[idx].domain.ty())
                    )));
                }
                updates.push(RUpdate {
                    target: idx,
                    target_name: vars[idx].name.clone(),
                    value: rhs,
                });
            }
            for (i, u) in updates.iter().enumerate() {
                if updates[..i].iter().any(|o| o.target == u.target) {
                    return Err(LangError::new(
                        "DUPLICATE_ASSIGNMENT",
                        format!(
                            "transition `{}` assigns variable `{}` more than once",
                            t.name, u.target_name
                        ),
                    ));
                }
            }
            transitions.push(RTransition {
                name: t.name.clone(),
                guard,
                updates,
            });
        }

        let mut properties = Vec::with_capacity(spec.properties.len());
        for p in &spec.properties {
            let predicate =
                resolve_pred(&p.predicate, format!("property `{}`", p.name))?;
            properties.push(RProperty {
                name: p.name.clone(),
                kind: p.kind,
                predicate,
            });
        }

        let digits: Vec<u128> = vars.iter().map(|v| v.domain.size()).collect();
        let strides = build_strides(&digits);

        Ok(CompiledSpec {
            name: spec.name.clone(),
            variables: spec.variables.clone(),
            initial,
            transitions,
            terminal,
            properties,
            digits,
            strides,
        })
    }

    pub fn width(&self) -> usize {
        self.variables.len()
    }

    pub fn total_states(&self) -> u128 {
        // stride of the first variable is the product of all other
        // dimensions; multiply by the first dimension for the full product.
        match self.strides.first() {
            Some(first_stride) => first_stride.saturating_mul(self.digits[0]),
            None => 0,
        }
    }

    /// Mixed-radix canonical id of a fully assigned, in-domain state.
    pub fn state_id(&self, state: &[Value]) -> Option<u128> {
        let mut id: u128 = 0;
        for (i, (var, value)) in self.variables.iter().zip(state).enumerate() {
            let d = var.domain.digit_for(value)?;
            id = id.checked_add(self.strides[i].checked_mul(d)?)?;
        }
        Some(id)
    }

    pub fn value_of<'a>(&self, state: &'a [Value], name: &str) -> Option<&'a Value> {
        self.variables
            .iter()
            .position(|v| v.name == name)
            .map(|i| &state[i])
    }

    /// Enumerate the complete Cartesian product of all variable domains.
    pub fn iter_domain(&self) -> DomainIter<'_> {
        DomainIter::new(self)
    }

    /// Apply a transition's update simultaneously: every RHS is evaluated
    /// against `pre` before any write. Out-of-domain results are reported as
    /// [`crate::error::StateError`] so they cannot silently alias another state.
    pub fn apply(
        &self,
        transition: &RTransition,
        pre: &[Value],
    ) -> Result<Vec<Value>, crate::error::StateError> {
        let mut next = pre.to_vec();
        let mut computed: Vec<(usize, Value)> = Vec::with_capacity(transition.updates.len());
        for u in &transition.updates {
            let value = u.value.eval(pre).map_err(|e| {
                crate::error::StateError::new(
                    &e.code,
                    &transition.name,
                    format!("updating `{}`: {}", u.target_name, e.message),
                )
            })?;
            computed.push((u.target, value));
        }
        for (idx, value) in computed {
            let domain = &self.variables[idx].domain;
            if !domain.contains(&value) {
                return Err(crate::error::StateError::new(
                    "VALUE_OUT_OF_DOMAIN",
                    &transition.name,
                    format!(
                        "assignment to `{}` produced {} outside its declared domain",
                        self.variables[idx].name,
                        value
                    ),
                ));
            }
            next[idx] = value;
        }
        Ok(next)
    }
}

fn build_strides(digits: &[u128]) -> Vec<u128> {
    // stride[i] = product of digits of variables AFTER i, so the last
    // declared variable (which the domain iterator varies fastest) has
    // weight 1 and enumeration produces contiguous ids. Saturating, since
    // ids are informational; structural dedup uses the value vector.
    let mut strides = vec![1u128; digits.len()];
    for i in (0..digits.len().saturating_sub(1)).rev() {
        strides[i] = strides[i + 1].saturating_mul(digits[i + 1]);
    }
    strides
}

/// Iterator over the full Cartesian product of variable domains, in the
/// declared variable order (last variable changes fastest).
pub struct DomainIter<'a> {
    spec: &'a CompiledSpec,
    digits: Vec<u128>,
    current: Vec<u128>,
    done: bool,
}

impl<'a> DomainIter<'a> {
    fn new(spec: &'a CompiledSpec) -> DomainIter<'a> {
        DomainIter {
            spec,
            digits: spec.digits.clone(),
            current: vec![0; spec.width()],
            done: false,
        }
    }

    fn materialize(&self) -> Vec<Value> {
        self.spec
            .variables
            .iter()
            .zip(&self.current)
            .map(|(v, d)| match &v.domain {
                Domain::IntRange { lo, .. } => Value::Int(lo + *d as i64),
                Domain::Bool => Value::Bool(*d == 1),
            })
            .collect()
    }
}

impl<'a> Iterator for DomainIter<'a> {
    type Item = Vec<Value>;

    fn next(&mut self) -> Option<Vec<Value>> {
        if self.done {
            return None;
        }
        let out = self.materialize();
        let mut i = self.current.len();
        loop {
            if i == 0 {
                self.done = true;
                break;
            }
            i -= 1;
            self.current[i] += 1;
            if self.current[i] < self.digits[i] {
                break;
            }
            self.current[i] = 0;
        }
        Some(out)
    }
}
