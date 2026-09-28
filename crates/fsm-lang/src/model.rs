//! Input language: declarative finite-state-machine specification model.
//!
//! A specification declares typed state variables with finite domains,
//! an initial-state predicate, transitions with guards and simultaneous
//! updates (all reads come from the same pre-state), an optional legal
//! termination predicate, and AG/EF properties.

use serde::{Deserialize, Serialize};

/// A runtime value. The language has two value types: signed integer and bool.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Value {
    Int(i64),
    Bool(bool),
}

impl Value {
    pub fn type_name(&self) -> &'static str {
        match self {
            Value::Int(_) => "int",
            Value::Bool(_) => "bool",
        }
    }

    pub fn as_int(&self) -> Option<i64> {
        match self {
            Value::Int(v) => Some(*v),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Value::Bool(v) => Some(*v),
            _ => None,
        }
    }
}

impl std::fmt::Display for Value {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Value::Int(v) => write!(f, "{}", v),
            Value::Bool(v) => write!(f, "{}", v),
        }
    }
}

/// Declared domain of a state variable.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Domain {
    /// Inclusive integer range `[lo, hi]`; requires `lo <= hi`.
    IntRange { lo: i64, hi: i64 },
    /// Booleans.
    Bool,
}

/// A declared state variable.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Variable {
    pub name: String,
    pub domain: Domain,
}

/// Expression AST. Encoded in JSON as `{"op": ..., ...}`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op")]
pub enum Expr {
    #[serde(rename = "const")]
    Const { value: Value },
    #[serde(rename = "var")]
    Var { name: String },

    #[serde(rename = "not")]
    Not { expr: Box<Expr> },
    #[serde(rename = "neg")]
    Neg { expr: Box<Expr> },

    #[serde(rename = "and")]
    And { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "or")]
    Or { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "implies")]
    Implies { left: Box<Expr>, right: Box<Expr> },

    #[serde(rename = "add")]
    Add { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "sub")]
    Sub { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "mul")]
    Mul { left: Box<Expr>, right: Box<Expr> },

    #[serde(rename = "eq")]
    Eq { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "ne")]
    Ne { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "lt")]
    Lt { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "le")]
    Le { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "gt")]
    Gt { left: Box<Expr>, right: Box<Expr> },
    #[serde(rename = "ge")]
    Ge { left: Box<Expr>, right: Box<Expr> },
}

/// A single variable assignment. Every assignment in a transition's update
/// is evaluated against the same pre-state and applied simultaneously.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Update {
    pub var: String,
    pub value: Expr,
}

/// A transition: enabled when `guard` holds; fires by applying `updates`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Transition {
    pub name: String,
    /// Defaults to boolean `true` when omitted.
    #[serde(default = "crate::compile::default_true")]
    pub guard: Expr,
    /// Defaults to an empty (identity) update when omitted.
    #[serde(default)]
    pub updates: Vec<Update>,
}

/// Property kind:
/// `ag` — invariant (AG predicate holds on every reachable state),
/// `ef` — reachability (EF predicate: some reachable state satisfies it).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PropertyKind {
    Ag,
    Ef,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Property {
    pub name: String,
    pub kind: PropertyKind,
    pub predicate: Expr,
}

/// The complete declarative specification (the input language).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Spec {
    pub name: String,
    #[serde(default)]
    pub variables: Vec<Variable>,
    /// Initial-state predicate. A state is initial iff it evaluates to true.
    /// At least one initial state must exist.
    pub initial: Expr,
    #[serde(default)]
    pub transitions: Vec<Transition>,
    /// Optional legal-termination predicate. A state satisfying it is a
    /// legal terminal state, distinct from a deadlock.
    #[serde(default)]
    pub terminal: Option<Expr>,
    #[serde(default)]
    pub properties: Vec<Property>,
}
