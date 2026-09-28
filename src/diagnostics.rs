//! Diagnostics support: request/job correlation ids and sensitive-data redaction.
//!
//! Constraint bodies may encode proprietary rules. By default logs never print
//! clauses — only non-sensitive identities and counts. Full formula logging is an
//! explicit opt-in via config.

use crate::language::Cnf;

/// Correlation id carried on every log line/error for a request. Taken from the
/// `X-Request-Id` header when supplied, otherwise generated.
#[derive(Debug, Clone)]
pub struct RequestId(pub String);

impl RequestId {
    #[must_use]
    pub fn new() -> Self {
        RequestId(format!("req_{}", uuid::Uuid::new_v4().simple()))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl Default for RequestId {
    fn default() -> Self {
        Self::new()
    }
}

impl std::fmt::Display for RequestId {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

/// Redactor guarding formula contents in logs/error payloads.
#[derive(Debug, Clone, Copy)]
pub struct Redactor {
    pub log_formulas: bool,
}

impl Redactor {
    #[must_use]
    pub fn new(log_formulas: bool) -> Self {
        Self { log_formulas }
    }

    /// Safe one-line description of a formula: ids and clause counts only.
    #[must_use]
    pub fn describe(&self, cnf: &Cnf) -> String {
        if self.log_formulas {
            // Opted in: show actual clauses but still cap the length.
            let s: Vec<String> = cnf
                .constraints
                .iter()
                .map(|c| {
                    let body: Vec<String> = c.literals.iter().map(|l| l.signed().to_string()).collect();
                    format!("{}: {}", c.id, body.join(" "))
                })
                .collect();
            let joined = s.join("; ");
            if joined.len() > 500 {
                format!("nvars={} [{} …]", cnf.nvars, &joined[..497])
            } else {
                format!("nvars={} [{joined}]", cnf.nvars)
            }
        } else {
            let ids: Vec<&str> = cnf.constraints.iter().map(|c| c.id.as_str()).take(10).collect();
            let more = if cnf.constraints.len() > 10 {
                format!(", +{} more", cnf.constraints.len() - 10)
            } else {
                String::new()
            };
            format!(
                "nvars={}, nconstraints={}, ids=[{}{}]",
                cnf.nvars,
                cnf.constraints.len(),
                ids.join(","),
                more
            )
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::language::{parse_cnf, Literal};

    #[test]
    fn redaction_hides_literals_by_default() {
        let cnf = parse_cnf("3\na: 1 2 0\nb: -3 0\n").unwrap();
        let r = Redactor::new(false);
        let d = r.describe(&cnf);
        assert!(d.contains("a"));
        assert!(!d.contains("1 2") && !d.contains("-3"));
        let _ = Literal(1);
    }

    #[test]
    fn opt_in_shows_literals() {
        let cnf = parse_cnf("3\na: 1 2 0\n").unwrap();
        let d = Redactor::new(true).describe(&cnf);
        assert!(d.contains("1 2"));
    }
}
