//! External CLI solver adapter.
//!
//! Talks to any locally installed binary speaking the classic DIMACS protocol
//! (minisat/compatible): the CNF is written to a temporary `.cnf` file and the
//! program is invoked as `<binary> [extra args...] <file>`, with stdout containing
//! `SATISFIABLE` / `UNSATISFIABLE` and, for SAT, `v <model> 0` lines.
//!
//! Any failure to spawn, abnormal termination, or unparseable verdict becomes
//! [`SStatus::Unknown`] with a diagnostic detail — never UNSAT. The command line is
//! configuration, not request data, and it alone is logged.

use super::{SStatus, SolveCtx, SolveResult, Solver};
use crate::language::{Cnf, Model};
use std::path::PathBuf;
use std::process::Command;

#[derive(Debug, Clone)]
pub struct ExternalCliSolver {
    binary: String,
    extra_args: Vec<String>,
    workdir: PathBuf,
}

impl ExternalCliSolver {
    /// `binary` is the solver executable; `extra_args` are inserted before the
    /// generated CNF file path.
    #[must_use]
    pub fn new(binary: impl Into<String>, extra_args: Vec<String>) -> Self {
        Self {
            binary: binary.into(),
            extra_args,
            workdir: std::env::temp_dir(),
        }
    }

    fn write_dimacs(&self, cnf: &Cnf) -> std::io::Result<PathBuf> {
        let mut path = self.workdir.clone();
        path.push(format!("mus-core-{}.cnf", uuid::Uuid::new_v4()));
        let mut body = String::new();
        body.push_str(&format!("p cnf {} {}\n", cnf.nvars, cnf.constraints.len()));
        for c in &cnf.constraints {
            // Constraint ids are recorded as comments to keep identities inspectable
            // in the generated file; the child only reads the numeric lines.
            body.push_str(&format!("c id {}\n", c.id));
            for l in &c.literals {
                body.push_str(&format!("{} ", l.signed()));
            }
            body.push_str("0\n");
        }
        std::fs::write(&path, body)?;
        Ok(path)
    }
}

impl Solver for ExternalCliSolver {
    fn name(&self) -> &str {
        "external-cli"
    }

    fn solve(&self, cnf: &Cnf, ctx: &SolveCtx) -> SolveResult {
        if !ctx.budget.tick() {
            return SolveResult::unknown("solver call budget exhausted before decision");
        }
        if ctx.cancel.is_cancelled() {
            return SolveResult::unknown("cancelled before spawning external solver");
        }

        let path = match self.write_dimacs(cnf) {
            Ok(p) => p,
            Err(e) => return SolveResult::unknown(format!("failed to write CNF tempfile: {e}")),
        };

        let output = Command::new(&self.binary).args(&self.extra_args).arg(&path).output();

        let _ = std::fs::remove_file(&path); // best-effort cleanup

        let output = match output {
            Ok(o) => o,
            Err(e) => {
                return SolveResult::unknown(format!("failed to spawn {}: {e}", self.binary));
            }
        };

        let stdout = String::from_utf8_lossy(&output.stdout);
        parse_dimacs_verdict(&stdout, cnf, &self.binary)
    }
}

fn parse_dimacs_verdict(stdout: &str, cnf: &Cnf, binary: &str) -> SolveResult {
    let has_sat = stdout.lines().any(|l| {
        let t = l.trim();
        t.eq_ignore_ascii_case("SAT") || t.eq_ignore_ascii_case("SATISFIABLE")
    });
    let has_unsat = stdout.lines().any(|l| {
        let t = l.trim();
        t.eq_ignore_ascii_case("UNSAT") || t.eq_ignore_ascii_case("UNSATISFIABLE")
    });

    match (has_sat, has_unsat) {
        (true, false) => {
            // Collect every `v` line into a model, then defensively verify it.
            let mut values: Vec<i32> = Vec::new();
            for line in stdout.lines() {
                let t = line.trim();
                if let Some(rest) = t.strip_prefix('v') {
                    values.extend(
                        rest.split_whitespace()
                            .filter_map(|tok| tok.parse::<i32>().ok())
                            .filter(|&n| n != 0),
                    );
                }
            }
            let model = model_from_values(&values, cnf.nvars);
            if cnf.satisfied_by(&model) {
                SolveResult {
                    status: SStatus::Sat,
                    model: Some(model),
                    detail: Some(format!("{binary} reported SAT; witness verified")),
                }
            } else {
                SolveResult::unknown(format!(
                    "{binary} reported SAT but its witness does not satisfy the formula"
                ))
            }
        }
        (false, true) => SolveResult {
            status: SStatus::Unsat,
            model: None,
            detail: Some(format!("{binary} reported UNSATISFIABLE")),
        },
        _ => SolveResult::unknown(format!(
            "{binary} produced no recognizable SAT/UNSAT verdict (stdout: {} byte(s))",
            stdout.len()
        )),
    }
}

/// Parse a `v`-line model returned by an external solver (unassigned vars → true).
#[must_use]
pub fn model_from_values(values: &[i32], nvars: usize) -> Model {
    let mut model = vec![true; nvars + 1];
    for &n in values {
        let v = n.unsigned_abs() as usize;
        if v == 0 || v > nvars {
            continue;
        }
        model[v] = n > 0;
    }
    Model(model)
}
