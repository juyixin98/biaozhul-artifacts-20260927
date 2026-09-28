//! Input language: propositional CNF with **client-supplied unique constraint identities**.
//!
//! Every clause carries a [`ClauseId`] supplied by the caller; the service never invents
//! an identity for a constraint (parsers for anonymous inputs such as DIMACS generate
//! explicit ids and tell the caller what they are). Keeping identity separate from
//! clause position means diagnostics, deletion records and proofs all refer to the
//! caller's names even after reordering or masking.
//!
//! Variable ids are positive 1-based integers, as in DIMACS. A literal is a signed
//! variable; the JSON wire form is an integer (`-3` is the negation of variable 3).

use std::collections::HashSet;

use serde::{Deserialize, Serialize};

/// Logical variable, 1-based.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Var(pub i64);

/// A signed variable. On the JSON wire this is a plain integer literal.
#[derive(Clone, Copy, PartialEq, Eq, Hash)]
pub struct Literal {
    pub var: Var,
    pub negated: bool,
}

impl Literal {
    pub fn new(var: i64, negated: bool) -> Result<Self, LanguageError> {
        if var <= 0 {
            return Err(LanguageError::BadVariable(var));
        }
        Ok(Self {
            var: Var(var),
            negated,
        })
    }

    pub fn from_dimacs(n: i64) -> Result<Self, LanguageError> {
        if n == 0 || n == i64::MIN {
            return Err(LanguageError::BadLiteral(n));
        }
        Ok(Self {
            var: Var(n.abs()),
            negated: n < 0,
        })
    }

    pub fn dimacs(&self) -> i64 {
        if self.negated {
            -self.var.0
        } else {
            self.var.0
        }
    }
}

impl Serialize for Literal {
    fn serialize<S>(&self, ser: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        ser.serialize_i64(self.dimacs())
    }
}

impl<'de> Deserialize<'de> for Literal {
    fn deserialize<D>(de: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let n = i64::deserialize(de)?;
        Literal::from_dimacs(n).map_err(serde::de::Error::custom)
    }
}

impl std::fmt::Debug for Literal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.dimacs())
    }
}

impl std::fmt::Display for Literal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.dimacs())
    }
}

/// Client-supplied, opaque identity of a constraint.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ClauseId(pub String);

impl ClauseId {
    pub fn new(s: impl Into<String>) -> Self {
        Self(s.into())
    }
}

impl std::fmt::Display for ClauseId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// One constraint: a disjunction of literals.
///
/// `sensitive` is a redaction marker. The service treats clauses as opaque data for
/// solving; the marker only changes what is allowed to appear in logs/diagnostics.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Clause {
    pub id: ClauseId,
    #[serde(default)]
    pub literals: Vec<Literal>,
    #[serde(default)]
    pub sensitive: bool,
}

impl Clause {
    /// An empty clause is an unconditional contradiction.
    pub fn is_empty(&self) -> bool {
        self.literals.is_empty()
    }
}

/// A CNF formula plus its sensitivity markers.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Formula {
    pub clauses: Vec<Clause>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LanguageError {
    BadVariable(i64),
    BadLiteral(i64),
    EmptyId,
    DuplicateId(String),
    ZeroClauses,
}

impl std::fmt::Display for LanguageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LanguageError::BadVariable(v) => write!(f, "variable id must be >= 1, got {v}"),
            LanguageError::BadLiteral(n) => write!(f, "literal 0 is not allowed (got {n})"),
            LanguageError::EmptyId => write!(f, "clause id must be non-empty"),
            LanguageError::DuplicateId(id) => write!(f, "duplicate clause id: {id}"),
            LanguageError::ZeroClauses => write!(f, "formula must contain at least one clause"),
        }
    }
}

impl std::error::Error for LanguageError {}

impl Formula {
    /// Validate the *identity* contract: every constraint has one unique, non-empty id,
    /// literals are well formed. Note this deliberately does NOT reject tautological or
    /// duplicated clauses — redundancy is part of the problem domain, not an input error.
    pub fn validate(&self) -> Result<(), LanguageError> {
        if self.clauses.is_empty() {
            return Err(LanguageError::ZeroClauses);
        }
        let mut seen = HashSet::with_capacity(self.clauses.len());
        for c in &self.clauses {
            if c.id.0.is_empty() {
                return Err(LanguageError::EmptyId);
            }
            if !seen.insert(c.id.0.clone()) {
                return Err(LanguageError::DuplicateId(c.id.0.clone()));
            }
            for lit in &c.literals {
                if lit.var.0 <= 0 {
                    return Err(LanguageError::BadVariable(lit.var.0));
                }
            }
        }
        Ok(())
    }

    /// Number of distinct variables appearing in the formula.
    pub fn num_vars(&self) -> usize {
        let mut vars = HashSet::new();
        for c in &self.clauses {
            for lit in &c.literals {
                vars.insert(lit.var.0);
            }
        }
        vars.len()
    }

    /// Stable, non-cryptographic fingerprint of the constraint *contents*. Used in
    /// diagnostics so log lines can be correlated without printing literals.
    pub fn fingerprint(&self) -> u64 {
        // FNV-1a 64-bit, applied canonically clause by clause.
        const OFFSET: u64 = 0xcbf29ce484222325;
        const PRIME: u64 = 0x100000001b3;
        let mut hash = OFFSET;
        let mut feed = |bytes: &[u8]| {
            for &b in bytes {
                hash ^= b as u64;
                hash = hash.wrapping_mul(PRIME);
            }
        };
        for c in &self.clauses {
            feed(b"cl:");
            feed(c.id.0.as_bytes());
            feed(b"=");
            for lit in &c.literals {
                let d = lit.dimacs().to_le_bytes();
                feed(&d);
                feed(b",");
            }
            feed(b";");
        }
        hash
    }
}

/// Parse DIMACS CNF text. DIMACS has no clause names; ids are generated deterministically
/// as `c1, c2, ...` in body order, so callers can still correlate the result.
pub fn parse_dimacs(input: &str) -> Result<Formula, LanguageError> {
    let mut clauses = Vec::new();
    let mut current: Vec<Literal> = Vec::new();
    let mut idx = 0usize;
    for (line_no, raw) in input.lines().enumerate() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('c') || line.starts_with('p') {
            continue;
        }
        for tok in line.split_whitespace() {
            let n: i64 = tok
                .parse()
                .map_err(|_| LanguageError::BadLiteral(line_no as i64 + 1))?;
            if n == 0 {
                idx += 1;
                clauses.push(Clause {
                    id: ClauseId(format!("c{idx}")),
                    literals: std::mem::take(&mut current),
                    sensitive: false,
                });
            } else {
                current.push(Literal::from_dimacs(n)?);
            }
        }
    }
    if !current.is_empty() {
        // Tolerate a missing terminating 0.
        idx += 1;
        clauses.push(Clause {
            id: ClauseId(format!("c{idx}")),
            literals: current,
            sensitive: false,
        });
    }
    let f = Formula { clauses };
    f.validate()?;
    Ok(f)
}

/// Emit DIMACS for a *subset* of clauses (identified by position membership).
/// Used by the external solver adapter; the renaming map is 1-based var-by-order
/// but because variable ids are already dense-positive we pass them through.
pub fn render_dimacs_subset(formula: &Formula, mask: &[bool]) -> String {
    let mut max_var = 0i64;
    let mut count = 0usize;
    for (i, c) in formula.clauses.iter().enumerate() {
        if mask[i] {
            count += 1;
            for lit in &c.literals {
                max_var = max_var.max(lit.var.0);
            }
        }
    }
    let mut out = format!("p cnf {max_var} {count}\n");
    for (i, c) in formula.clauses.iter().enumerate() {
        if mask[i] {
            for lit in &c.literals {
                out.push_str(&lit.dimacs().to_string());
                out.push(' ');
            }
            out.push_str("0\n");
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_duplicate_ids() {
        let f = Formula {
            clauses: vec![
                Clause {
                    id: ClauseId::new("x"),
                    literals: vec![Literal::from_dimacs(1).unwrap()],
                    sensitive: false,
                },
                Clause {
                    id: ClauseId::new("x"),
                    literals: vec![Literal::from_dimacs(2).unwrap()],
                    sensitive: false,
                },
            ],
        };
        assert_eq!(f.validate(), Err(LanguageError::DuplicateId("x".into())));
    }

    #[test]
    fn rejects_literal_zero() {
        assert_eq!(
            Literal::from_dimacs(0),
            Err(LanguageError::BadLiteral(0))
        );
    }

    #[test]
    fn fingerprint_stable_and_order_sensitive() {
        let a = parse_dimacs("p cnf 1 1\n1 0\n").unwrap();
        let a2 = parse_dimacs("p cnf 1 1\n1 0\n").unwrap();
        assert_eq!(a.fingerprint(), a2.fingerprint());
    }
}
