use std::fmt;

use crate::ast::{Assign, Expr};

/// A runtime value. Booleans and integers are kept in one enum so a single
/// expression evaluator can cover all variable domains.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Value {
    Bool(bool),
    Int(i64),
}

impl Value {
    #[allow(clippy::result_unit_err)]
    pub fn as_bool(self) -> Result<bool, ()> {
        match self {
            Value::Bool(b) => Ok(b),
            _ => Err(()),
        }
    }

    #[allow(clippy::result_unit_err)]
    pub fn as_int(self) -> Result<i64, ()> {
        match self {
            Value::Int(i) => Ok(i),
            _ => Err(()),
        }
    }

    pub fn type_name(self) -> &'static str {
        match self {
            Value::Bool(_) => "bool",
            Value::Int(_) => "int",
        }
    }
}

impl fmt::Display for Value {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Value::Bool(b) => write!(f, "{b}"),
            Value::Int(i) => write!(f, "{i}"),
        }
    }
}

/// Finite domain of a state variable.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Domain {
    Bool,
    /// Inclusive integer range `[lo, hi]`, with `lo <= hi`.
    IntRange {
        lo: i64,
        hi: i64,
    },
    /// Enumeration with named variants. Variants resolve as `Value::Int`
    /// constants 0..n-1 in declaration order.
    Enum {
        variants: Vec<String>,
    },
}

impl Domain {
    /// Number of values in the domain. `None` on overflow (pathological
    /// 64-bit-wide ranges); such a domain is rejected at build time.
    pub fn size(&self) -> Option<u128> {
        match self {
            Domain::Bool => Some(2),
            Domain::IntRange { lo, hi } => {
                if hi < lo {
                    return Some(0);
                }
                let span = (*hi as i128) - (*lo as i128) + 1;
                u128::try_from(span).ok()
            }
            Domain::Enum { variants } => Some(variants.len() as u128),
        }
    }

    /// Mixed-radix stride (number of values). `None` on overflow.
    pub fn stride(&self) -> Option<u64> {
        u64::try_from(self.size()?).ok()
    }

    pub fn contains(&self, v: &Value) -> bool {
        match (self, v) {
            (Domain::Bool, Value::Bool(_)) => true,
            (Domain::IntRange { lo, hi }, Value::Int(i)) => i >= lo && i <= hi,
            (Domain::Enum { variants }, Value::Int(i)) => *i >= 0 && (*i as usize) < variants.len(),
            _ => false,
        }
    }
}

/// Declared state variable.
#[derive(Debug, Clone)]
pub struct Var {
    pub name: String,
    pub domain: Domain,
    /// Mixed-radix stride used by the codec: product of sizes of variables
    /// declared *after* this one.
    pub stride: u64,
}

/// Type-checked transition.
#[derive(Debug, Clone)]
pub struct Transition {
    pub name: String,
    pub guard: Expr,
    /// Parallel assignment; RHS expressions are evaluated against the
    /// pre-state before any value is committed.
    pub assign: Vec<Assign>,
}

/// A fully type-checked finite-state-machine specification.
#[derive(Debug, Clone)]
pub struct System {
    pub name: Option<String>,
    pub vars: Vec<Var>,
    /// Predicate selecting the initial state(s). Exactly one is required for
    /// the common case; several initial states model nondeterministic startup.
    pub init: Expr,
    pub transitions: Vec<Transition>,
    /// Legal-termination predicate. A state matching it is *not* a deadlock
    /// even when no guard is enabled.
    pub terminal: Expr,
    /// When the spec supplied an explicit concrete initial state, that one
    /// valuation is recorded here so the solver need not scan the whole
    /// product to find it. `None` means the initial set is given by a
    /// predicate and must be enumerated.
    pub concrete_init: Option<Vec<Value>>,
}

impl System {
    /// Lookup a variable by name.
    pub fn var_index(&self, name: &str) -> Option<usize> {
        self.vars.iter().position(|v| v.name == name)
    }

    /// Fresh state vector (values default to `Bool(false)`; callers fill it).
    pub fn fresh_state(&self) -> Vec<Value> {
        vec![Value::Bool(false); self.vars.len()]
    }

    /// Number of reachable-state budgets worth of valuation space.
    pub fn total_space(&self) -> u128 {
        self.vars.iter().fold(1u128, |acc, v| {
            acc.saturating_mul(v.domain.size().unwrap_or(0))
        })
    }
}
