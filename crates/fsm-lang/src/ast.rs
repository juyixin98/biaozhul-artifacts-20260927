use serde::Serialize;

/// Typed expression AST. Identifiers are resolved to variable indices during
/// type checking; enum variant spellings survive until evaluation and resolve
/// through the [`crate::System`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Expr {
    Bool(bool),
    Int(i64),
    /// `name` is the original spelling; `res` is filled by the builder:
    /// `Some((var_index))` for variables, enum variants stay unresolved here
    /// and are looked up while evaluating.
    Name {
        name: String,
        res: Option<usize>,
    },
    Unary {
        op: UnOp,
        e: Box<Expr>,
    },
    Binary {
        op: BinOp,
        l: Box<Expr>,
        r: Box<Expr>,
    },
    /// Conditional `if c then a else b`.
    Ite {
        cond: Box<Expr>,
        thn: Box<Expr>,
        els: Box<Expr>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnOp {
    Not,
    Neg,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BinOp {
    // arithmetic
    Add,
    Sub,
    Mul,
    Div,
    Mod,
    // comparison
    Lt,
    Le,
    Gt,
    Ge,
    Eq,
    Ne,
    // boolean
    And,
    Or,
}

impl BinOp {
    pub fn symbol(self) -> &'static str {
        match self {
            BinOp::Add => "+",
            BinOp::Sub => "-",
            BinOp::Mul => "*",
            BinOp::Div => "/",
            BinOp::Mod => "mod",
            BinOp::Lt => "<",
            BinOp::Le => "<=",
            BinOp::Gt => ">",
            BinOp::Ge => ">=",
            BinOp::Eq => "==",
            BinOp::Ne => "!=",
            BinOp::And => "&&",
            BinOp::Or => "||",
        }
    }
}

/// Parallel assignment `target := rhs`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Assign {
    pub target: String,
    /// Filled by the builder.
    pub target_index: Option<usize>,
    pub rhs: Expr,
}

/// Raw transition before type checking.
#[derive(Debug, Clone)]
pub struct RawTransition {
    pub name: String,
    pub guard: Expr,
    pub assign: Vec<Assign>,
}

/// Raw specification after parsing, before type checking.
#[derive(Debug, Clone)]
pub struct RawSystem {
    pub name: Option<String>,
    pub vars: Vec<(String, crate::Domain)>,
    /// `None` means the initial state was given as an explicit concrete state.
    pub init_predicate: Option<Expr>,
    /// Used when `init_predicate` is `None`: conjunction of equalities.
    pub init_state: Vec<(String, Expr)>,
    pub transitions: Vec<RawTransition>,
    /// Multiple `terminal` clauses are combined with OR.
    pub terminals: Vec<Expr>,
}

/// Serializable classification of build failures. The API and CLI render
/// these verbatim so failure reasons are never just "an error happened".
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BuildErrorKind {
    /// Lexical error.
    Lex,
    /// Grammatical error.
    Parse,
    /// Duplicate variable / transition / enum-variant name.
    DuplicateName,
    /// Empty or contradictory declaration (e.g. `int 2..1`).
    InvalidDomain,
    /// Integer domain so wide the mixed-radix codec would overflow.
    StateSpaceOverflow,
    /// Reference to an unknown name.
    UnknownName,
    /// Enum variant name declared by two enums.
    AmbiguousEnumVariant,
    /// An assignment targets the same variable twice in one transition.
    DuplicateAssignment,
    /// A transition assigns to an undeclared variable.
    UnknownAssignmentTarget,
    /// Concrete initial state did not set every variable.
    MissingVariable,
    /// Type mismatch / non-boolean predicate.
    TypeMismatch,
    /// JSON document is malformed or has the wrong shape.
    InvalidJson,
}
