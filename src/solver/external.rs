//! Adapter for external DIMACS CNF SAT solver binaries.
//!
//! The adapter writes the selected subset to a temporary `.cnf` file, runs the
//! configured command and parses standard solver output:
//!
//! ```text
//! s SATISFIABLE        (or a bare "SATISFIABLE" line)
//! s UNSATISFIABLE
//! ```
//!
//! Satisfying assignments are parsed from `v ...` lines when present. Anything else —
//! timeout, non-UTF8 output, missing verdict line, spawn failure — becomes `Unknown`
//! with a diagnostic reason. It is never silently promoted to UNSAT.
//!
//! Configure with a command template containing the placeholder `{in}`, e.g.
//! `kissat {in}` or `minisat {in} /dev/stdout`. Because spawning arbitrary commands
//! is an operator decision, this solver is only created when explicitly configured.

use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

use crate::language::Formula;

use super::{SolveLimits, SolveOutcome, SolveStatus, SatSolver};

#[derive(Debug, Clone)]
pub struct ExternalSolver {
    pub solver_name: String,
    /// Argument vector; the exact token `{in}` is replaced with the temp CNF path.
    pub argv: Vec<String>,
}

impl ExternalSolver {
    pub fn new(solver_name: impl Into<String>, argv: Vec<String>) -> Self {
        Self {
            solver_name: solver_name.into(),
            argv,
        }
    }

    /// Parse `s SATISFIABLE` / bare `SATISFIABLE` lines and optional `v` model lines.
    /// Package-private visibility for unit testing without spawning processes.
    pub(crate) fn parse_output(stdout: &str) -> (SolveStatus, Option<Vec<i64>>) {
        let mut verdict: Option<SolveStatus> = None;
        let mut model_toks: Vec<i64> = Vec::new();
        for line in stdout.lines() {
            let line = line.trim();
            if let Some(rest) = line.strip_prefix("s ") {
                let r = rest.trim();
                if r.eq_ignore_ascii_case("SATISFIABLE") {
                    verdict = Some(SolveStatus::Sat);
                } else if r.eq_ignore_ascii_case("UNSATISFIABLE") {
                    verdict = Some(SolveStatus::Unsat);
                } else if r.eq_ignore_ascii_case("UNKNOWN") {
                    verdict.get_or_insert(SolveStatus::Unknown);
                }
            } else if line.eq_ignore_ascii_case("SATISFIABLE") {
                verdict = Some(SolveStatus::Sat);
            } else if line.eq_ignore_ascii_case("UNSATISFIABLE") {
                verdict = Some(SolveStatus::Unsat);
            } else if let Some(rest) = line.strip_prefix("v ") {
                for tok in rest.split_whitespace() {
                    if let Ok(n) = tok.parse::<i64>() {
                        if n != 0 {
                            model_toks.push(n);
                        }
                    }
                }
            }
        }
        let model = if verdict == Some(SolveStatus::Sat) && !model_toks.is_empty() {
            Some(model_toks)
        } else {
            None
        };
        (verdict.unwrap_or(SolveStatus::Unknown), model)
    }
}

impl SatSolver for ExternalSolver {
    fn name(&self) -> &str {
        &self.solver_name
    }

    fn solve(
        &self,
        formula: &Formula,
        mask: &[bool],
        limits: &SolveLimits,
        cancel: Option<&AtomicBool>,
    ) -> SolveOutcome {
        if self.argv.is_empty() {
            return SolveOutcome::unknown("external solver command is empty", 0);
        }
        let cnf = crate::language::render_dimacs_subset(formula, mask);
        let dir = std::env::temp_dir();
        let path = dir.join(format!(
            "mus-service-{}-{}.cnf",
            std::process::id(),
            uuid::Uuid::new_v4().simple()
        ));
        if let Err(e) = std::fs::write(&path, cnf.as_bytes()) {
            return SolveOutcome::unknown(format!("failed to write temp CNF: {e}"), 0);
        }

        let program = &self.argv[0];
        let args: Vec<String> = self
            .argv
            .iter()
            .skip(1)
            .map(|a| a.replace("{in}", path.to_string_lossy().as_ref()))
            .collect();

        let mut child = match std::process::Command::new(program)
            .args(&args)
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null())
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                let _ = std::fs::remove_file(&path);
                return SolveOutcome::unknown(format!("failed to spawn {program}: {e}"), 0);
            }
        };

        let start = Instant::now();
        let timeout = limits.timeout_ms.unwrap_or(30_000);
        let outcome = loop {
            match child.try_wait() {
                Ok(Some(_status)) => break child.wait_with_output(),
                Ok(None) => {
                    if start.elapsed().as_millis() as u64 > timeout {
                        let _ = child.kill();
                        let _ = child.wait();
                        let _ = std::fs::remove_file(&path);
                        return SolveOutcome::unknown(
                            format!("external solver timed out after {timeout}ms"),
                            0,
                        );
                    }
                    if let Some(flag) = cancel {
                        if flag.load(Ordering::Relaxed) {
                            let _ = child.kill();
                            let _ = child.wait();
                            let _ = std::fs::remove_file(&path);
                            return SolveOutcome::unknown("cancellation requested", 0);
                        }
                    }
                    std::thread::sleep(std::time::Duration::from_millis(5));
                }
                Err(e) => {
                    let _ = std::fs::remove_file(&path);
                    return SolveOutcome::unknown(format!("error waiting on solver: {e}"), 0);
                }
            }
        };
        let _ = std::fs::remove_file(&path);

        let output = match outcome {
            Ok(o) => o,
            Err(e) => {
                return SolveOutcome::unknown(format!("failed to collect solver output: {e}"), 0)
            }
        };
        let stdout = match String::from_utf8(output.stdout) {
            Ok(s) => s,
            Err(_) => return SolveOutcome::unknown("solver output was not UTF-8", 0),
        };
        let (status, model_toks) = Self::parse_output(&stdout);
        match status {
            SolveStatus::Sat => {
                let model = model_toks.map(|toks| {
                    toks.into_iter()
                        .map(|l| (l.abs(), l > 0))
                        .collect::<std::collections::BTreeMap<_, _>>()
                });
                SolveOutcome {
                    status,
                    model,
                    decisions: 0,
                    reason: None,
                }
            }
            other => SolveOutcome {
                status: other,
                model: None,
                decisions: 0,
                reason: if other == SolveStatus::Unknown {
                    Some("external solver printed no parseable SAT/UNSAT verdict".to_string())
                } else {
                    None
                },
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_minisat_style_output() {
        let (s, m) = ExternalSolver::parse_output(
            "c something\ns SATISFIABLE\nv -1 2 3 0\n",
        );
        assert_eq!(s, SolveStatus::Sat);
        let m = m.unwrap();
        assert_eq!(m, vec![-1, 2, 3]);
    }

    #[test]
    fn parses_kissat_style_unsat() {
        let (s, _) = ExternalSolver::parse_output("c header\ns UNSATISFIABLE\n");
        assert_eq!(s, SolveStatus::Unsat);
    }

    #[test]
    fn garbage_output_is_unknown() {
        let (s, _) = ExternalSolver::parse_output("segmentation fault vibes");
        assert_eq!(s, SolveStatus::Unknown);
    }
}
