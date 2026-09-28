//! Input language: constraint identity, CNF representation, parsing and validation.
//!
//! Two input syntaxes are supported (see [`parse_cnf`]):
//!
//! * A local line-based format where each constraint (clause) carries an explicit,
//!   stable identity:
//!
//!   ```text
//!   # comment
//!   3              # optional: number of variables
//!   boot: -1 -2 0
//!   conflict_a: 1 2 0
//!   ```
//!
//! * Standard DIMACS CNF (`p cfn nvars nclauses` header). DIMACS clauses have no
//!   user-supplied identity, so stable ids `c1`, `c2`, ... are assigned in textual
//!   order.
//!
//! Design rule: every input constraint keeps its unique identity through the whole
//! pipeline (solver calls, deletion trace, output cores). Duplicate ids and other
//! malformed input are rejected at parse time rather than silently merged.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeSet, HashSet};

/// A signed SAT literal. Variable numbers are 1-based DIMACS integers; the sign
/// carries the polarity (`-3` = literal ¬x₃).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Literal(pub i32);

impl Literal {
    /// Variable number (always positive).
    #[must_use]
    pub fn var(self) -> usize {
        self.0.unsigned_abs() as usize
    }

    #[must_use]
    pub fn signed(self) -> i32 {
        self.0
    }

    #[must_use]
    pub fn is_positive(self) -> bool {
        self.0 > 0
    }
}

impl std::ops::Neg for Literal {
    type Output = Literal;
    fn neg(self) -> Literal {
        Literal(-self.0)
    }
}

/// One input constraint with its unique identity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Constraint {
    pub id: String,
    pub literals: Vec<Literal>,
}

/// A whole formula. `nvars` is the (validated) variable universe; clause ids are unique.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Cnf {
    pub nvars: usize,
    pub constraints: Vec<Constraint>,
}

/// A truth assignment, indexed by 1-based variable number (slot 0 unused).
/// `model[v] == true` means variable `v` is assigned true.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct Model(pub Vec<bool>);

impl Model {
    #[must_use]
    pub fn value(&self, lit: Literal) -> bool {
        self.0[lit.var()] ^ (!lit.is_positive())
    }
}

/// Parse-time failures. Each variant maps to a stable error code on the API.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ParseError {
    /// A clause referenced a variable outside the declared universe.
    VariableOutOfRange { clause_id: String, literal: i32, nvars: usize },
    /// A clause id appeared more than once.
    DuplicateId(String),
    /// A clause contained the same literal twice.
    DuplicateLiteral { clause_id: String, literal: i32 },
    /// A line could not be tokenized into integers (DIMACS body).
    BadInteger { clause_id: String, token: String },
    /// The `p cnf ...` header was malformed.
    BadHeader(String),
    /// The declared variable count was invalid (e.g. negative).
    BadNvarDecl(String),
    /// Local-format `id: 1 2 0` line was malformed.
    BadClauseLine(usize, String),
}

impl ParseError {
    #[must_use]
    pub fn code(&self) -> &'static str {
        match self {
            ParseError::VariableOutOfRange { .. } => "variable_out_of_range",
            ParseError::DuplicateId(_) => "duplicate_constraint_id",
            ParseError::DuplicateLiteral { .. } => "duplicate_literal",
            ParseError::BadInteger { .. } => "bad_integer",
            ParseError::BadHeader(_) => "bad_dimacs_header",
            ParseError::BadNvarDecl(_) => "bad_nvar_declaration",
            ParseError::BadClauseLine(_, _) => "bad_clause_line",
        }
    }
}

impl std::fmt::Display for ParseError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ParseError::VariableOutOfRange { clause_id, literal, nvars } => write!(
                f,
                "clause {clause_id:?} references variable {literal} outside declared universe of {nvars}"
            ),
            ParseError::DuplicateId(id) => write!(f, "duplicate constraint id {id:?}"),
            ParseError::DuplicateLiteral { clause_id, literal } => {
                write!(f, "clause {clause_id:?} repeats literal {literal}")
            }
            ParseError::BadInteger { clause_id, token } => {
                write!(f, "clause {clause_id:?} contains non-integer token {token:?}")
            }
            ParseError::BadHeader(msg) => write!(f, "bad DIMACS header: {msg}"),
            ParseError::BadNvarDecl(msg) => write!(f, "bad variable declaration: {msg}"),
            ParseError::BadClauseLine(line, msg) => {
                write!(f, "malformed clause on line {line}: {msg}")
            }
        }
    }
}

impl std::error::Error for ParseError {}

/// Parse either the local id-annotated format or standard DIMACS.
///
/// Format detection is structural: a line beginning with `p ` is treated as a
/// DIMACS header; otherwise clauses are expected as `id: lits... 0`. A leading
/// bare integer line declares the variable count in the local format.
pub fn parse_cnf(input: &str) -> Result<Cnf, ParseError> {
    let trimmed_non_comment = input.lines().any(|l| {
        let s = l.trim_start();
        s.starts_with("p ") || s.starts_with("p\t")
    });

    if trimmed_non_comment {
        parse_dimacs(input)
    } else {
        parse_local(input)
    }
}

fn parse_local(input: &str) -> Result<Cnf, ParseError> {
    let mut declared_nvars: Option<usize> = None;
    let mut constraints: Vec<Constraint> = Vec::new();
    let mut seen_ids: HashSet<String> = HashSet::new();

    for (idx, raw_line) in input.lines().enumerate() {
        let line_no = idx + 1;
        let line = strip_hash_comment(raw_line).trim();
        if line.is_empty() {
            continue;
        }

        let Some((head, body)) = line.split_once(':') else {
            // A bare integer line is the variable-count declaration.
            if constraints.is_empty() && declared_nvars.is_none() && line.split_whitespace().count() == 1 {
                if let Ok(n) = line.parse::<i64>() {
                    if n < 0 {
                        return Err(ParseError::BadNvarDecl(line.to_string()));
                    }
                    declared_nvars = Some(n as usize);
                    continue;
                }
            }
            return Err(ParseError::BadClauseLine(
                line_no,
                "expected `id: literals... 0` or a variable-count line".to_string(),
            ));
        };

        let id = head.trim().to_string();
        if id.is_empty() {
            return Err(ParseError::BadClauseLine(line_no, "empty constraint id".to_string()));
        }
        if !seen_ids.insert(id.clone()) {
            return Err(ParseError::DuplicateId(id));
        }

        let lits = parse_literal_tokens(body, line_no, &id)?;
        constraints.push(Constraint { id, literals: lits });
    }

    let nvars = declared_nvars
        .unwrap_or_else(|| constraints.iter().flat_map(|c| c.literals.iter()).map(|l| l.var()).max().unwrap_or(0));
    validate(Cnf { nvars, constraints })
}

fn parse_dimacs(input: &str) -> Result<Cnf, ParseError> {
    let mut nvars: Option<usize> = None;
    let mut pending: Vec<i32> = Vec::new();
    let mut constraints: Vec<Constraint> = Vec::new();
    let mut clause_index = 0usize;
    let mut header_seen = false;

    let finish_clause =
        |pending: &mut Vec<i32>, constraints: &mut Vec<Constraint>, clause_index: &mut usize| {
            *clause_index += 1;
            let id = format!("c{clause_index}");
            let lits = std::mem::take(pending)
                .into_iter()
                .map(Literal)
                .collect();
            constraints.push(Constraint { id, literals: lits });
        };

    for raw_line in input.lines() {
        let line = raw_line.trim();
        if line.is_empty() {
            continue;
        }
        if let Some(rest) = line.strip_prefix("p") {
            if rest.starts_with([' ', '\t']) {
                let parts: Vec<&str> = line.split_whitespace().collect();
                // p cnf <nvars> <nclauses>
                if parts.len() != 4 || parts[1] != "cnf" {
                    return Err(ParseError::BadHeader(line.to_string()));
                }
                let n: i64 = parts[2]
                    .parse()
                    .map_err(|_| ParseError::BadHeader(line.to_string()))?;
                if n < 0 {
                    return Err(ParseError::BadNvarDecl(parts[2].to_string()));
                }
                nvars = Some(n as usize);
                header_seen = true;
                continue;
            }
        }
        if !header_seen {
            // Comments before the header are allowed (`c ...` or `# ...`); anything else is an error.
            if line.starts_with('c') || line.starts_with('#') {
                continue;
            }
            return Err(ParseError::BadHeader("clause data before `p cnf` header".to_string()));
        }
        if line.starts_with('c') || line.starts_with('#') {
            continue;
        }

        for tok in line.split_whitespace() {
            let n: i32 = tok.parse().map_err(|_| ParseError::BadInteger {
                clause_id: format!("c{}", clause_index + 1),
                token: tok.to_string(),
            })?;
            if n == 0 {
                finish_clause(&mut pending, &mut constraints, &mut clause_index);
            } else {
                pending.push(n);
            }
        }
    }
    // Unterminated final clause: tolerate it (terminated at EOF).
    if !pending.is_empty() {
        finish_clause(&mut pending, &mut constraints, &mut clause_index);
    }

    let nvars = nvars.ok_or_else(|| ParseError::BadHeader("missing variable count".to_string()))?;
    validate(Cnf { nvars, constraints })
}

fn parse_literal_tokens(body: &str, line_no: usize, id: &str) -> Result<Vec<Literal>, ParseError> {
    let mut lits = Vec::new();
    let mut seen = HashSet::new();
    for tok in body.split_whitespace() {
        if tok == "0" {
            break;
        }
        let n: i32 = tok
            .parse()
            .map_err(|_| ParseError::BadClauseLine(line_no, format!("token {tok:?} is not an integer")))?;
        if n == 0 {
            unreachable!();
        }
        if !seen.insert(n) {
            return Err(ParseError::DuplicateLiteral { clause_id: id.to_string(), literal: n });
        }
        lits.push(Literal(n));
    }
    Ok(lits)
}

fn validate(cnf: Cnf) -> Result<Cnf, ParseError> {
    for c in &cnf.constraints {
        for l in &c.literals {
            if l.var() == 0 || l.var() > cnf.nvars {
                return Err(ParseError::VariableOutOfRange {
                    clause_id: c.id.clone(),
                    literal: l.0,
                    nvars: cnf.nvars,
                });
            }
        }
    }
    Ok(cnf)
}

fn strip_hash_comment(line: &str) -> &str {
    match line.find('#') {
        Some(i) => &line[..i],
        None => line,
    }
}

impl Cnf {
    /// Build a CNF from JSON-supplied constraints, applying the same validation
    /// (unique ids, variable universe, no repeated literals).
    pub fn from_constraints(
        nvars: Option<usize>,
        constraints: Vec<Constraint>,
    ) -> Result<Cnf, ParseError> {
        let mut seen: HashSet<String> = HashSet::new();
        for c in &constraints {
            if !seen.insert(c.id.clone()) {
                return Err(ParseError::DuplicateId(c.id.clone()));
            }
            let mut lits = HashSet::new();
            for l in &c.literals {
                if !lits.insert(l.0) {
                    return Err(ParseError::DuplicateLiteral {
                        clause_id: c.id.clone(),
                        literal: l.0,
                    });
                }
            }
        }
        let inferred = constraints
            .iter()
            .flat_map(|c| c.literals.iter())
            .map(|l| l.var())
            .max()
            .unwrap_or(0);
        let nvars = nvars.unwrap_or(inferred);
        validate(Cnf { nvars, constraints })
    }

    /// Restrict the formula to the constraints whose id is in `ids`, preserving order.
    #[must_use]
    pub fn subset(&self, ids: &BTreeSet<String>) -> Cnf {
        Cnf {
            nvars: self.nvars,
            constraints: self
                .constraints
                .iter()
                .filter(|c| ids.contains(&c.id))
                .cloned()
                .collect(),
        }
    }

    /// True if `model` satisfies every clause in this formula.
    #[must_use]
    pub fn satisfied_by(&self, model: &Model) -> bool {
        self.constraints
            .iter()
            .all(|c| c.literals.iter().any(|l| model.value(*l)))
    }

    /// Ids in textual order.
    pub fn ids(&self) -> impl Iterator<Item = &str> {
        self.constraints.iter().map(|c| c.id.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_local_format_with_identities() {
        let cnf = parse_cnf("3\nboot: -1 -2 0\na: 1 0\n").unwrap();
        assert_eq!(cnf.nvars, 3);
        assert_eq!(cnf.ids().collect::<Vec<_>>(), vec!["boot", "a"]);
        assert_eq!(cnf.constraints[0].literals, vec![Literal(-1), Literal(-2)]);
    }

    #[test]
    fn parses_dimacs_and_assigns_stable_ids() {
        let cnf = parse_cnf("p cnf 2 2\n-1 -2 0\n1 0\n").unwrap();
        assert_eq!(cnf.ids().collect::<Vec<_>>(), vec!["c1", "c2"]);
    }

    #[test]
    fn rejects_duplicate_identities() {
        let err = parse_cnf("2\na: 1 0\na: 2 0\n").unwrap_err();
        assert_eq!(err.code(), "duplicate_constraint_id");
    }

    #[test]
    fn rejects_variable_out_of_range() {
        let err = parse_cnf("1\na: 2 0\n").unwrap_err();
        assert_eq!(err.code(), "variable_out_of_range");
    }

    #[test]
    fn rejects_duplicate_literal() {
        let err = parse_cnf("2\na: 1 1 0\n").unwrap_err();
        assert_eq!(err.code(), "duplicate_literal");
    }

    #[test]
    fn model_evaluation_respects_polarity() {
        let m = Model(vec![false, true, false]); // x1=true, x2=false
        assert!(m.value(Literal(1)));
        assert!(m.value(Literal(-2)));
        assert!(!m.value(Literal(2)));
    }
}
