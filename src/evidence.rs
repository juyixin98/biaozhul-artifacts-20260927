//! Evidence bundles and verification.
//!
//! An [`EvidenceBundle`] records everything needed to replay a monitoring
//! run without the live server: the rule set, the trace, an optional snapshot
//! taken at a cut point, the obligations reported at that cut, and the
//! claimed final verdict.  Verification never trusts the recorded verdict:
//! it recomputes the trace through the independent offline oracle
//! (`oracle.rs`) and also re-drives the restored snapshot through the online
//! kernel, then compares all of them.

use serde::{Deserialize, Serialize};

use crate::error::{AppError, AppResult};
use crate::kernel::{hash_ruleset, Monitor, ObligationView, SavedMonitor, Verdict};
use crate::language::{Ruleset, Step};
use crate::oracle::{self, evaluate, OracleReport};

/// A replayable record of one run.
///
/// When `snapshot` is present it is taken after exactly
/// `snapshot_after_index` steps, and `online_obligations` describes the state
/// at that cut.  The full trace is still included so the suffix can be
/// re-driven from the restored state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct EvidenceBundle {
    /// Run id carried by the API call that produced this evidence; used to
    /// join evidence with test logs.
    pub run_id: String,
    pub ruleset: Ruleset,
    pub trace: Vec<Step>,
    /// Number of steps applied when the snapshot was taken.
    pub snapshot_after_index: Option<u64>,
    pub snapshot: Option<SavedMonitor>,
    /// Obligations reported by the online run at the cut (or at the end when
    /// no cut is given).
    pub online_obligations: Vec<ObligationView>,
    pub claimed_verdict: Verdict,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CheckFailure {
    pub check: &'static str,
    pub detail: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EvidenceReport {
    pub valid: bool,
    pub failures: Vec<CheckFailure>,
    pub oracle_verdict: Verdict,
    pub kernel_verdict: Verdict,
    pub restored_verdict: Option<Verdict>,
    pub obligation_count: usize,
}

fn compare_obligations(
    label: &'static str,
    online: &[ObligationView],
    report: &OracleReport,
    failures: &mut Vec<CheckFailure>,
) {
    let online_map: std::collections::BTreeMap<_, _> =
        online.iter().map(|v| (v.id.clone(), oracle::view_key(v))).collect();
    for o in &report.obligations {
        match online_map.get(&o.id) {
            Some(key) if *key == o.comparison_key() => {}
            Some(key) => failures.push(CheckFailure {
                check: label,
                detail: format!(
                    "obligation {}: oracle {:?} vs online {:?}",
                    o.id,
                    o.comparison_key(),
                    key
                ),
            }),
            None => failures.push(CheckFailure {
                check: label,
                detail: format!("missing online obligation {}", o.id),
            }),
        }
    }
    let oracle_ids: std::collections::HashSet<_> =
        report.obligations.iter().map(|o| o.id.clone()).collect();
    for id in online_map.keys() {
        if !oracle_ids.contains(id) {
            failures.push(CheckFailure {
                check: label,
                detail: format!("online reported phantom obligation {id}"),
            });
        }
    }
}

impl EvidenceBundle {
    /// Verify the bundle.  Invalid input is an [`AppError`]; a mismatch
    /// produces an `EvidenceReport` with `valid == false`.
    pub fn verify(&self) -> AppResult<EvidenceReport> {
        let mut failures = Vec::new();

        self.ruleset.validate()?;
        oracle::check_trace_shape(&self.trace)?;
        let hash = hash_ruleset(&self.ruleset);

        // 1) independent replay of the whole trace.
        let full_report = evaluate(&self.ruleset, &self.trace)?;
        if full_report.verdict != self.claimed_verdict {
            failures.push(CheckFailure {
                check: "claimed_verdict_vs_oracle",
                detail: format!(
                    "claimed {}, oracle {}",
                    self.claimed_verdict.as_str(),
                    full_report.verdict.as_str()
                ),
            });
        }

        // 2) a fresh online kernel run over the whole trace must agree with
        // both the verdict and every obligation.
        let limits = self
            .snapshot
            .as_ref()
            .map(|s| s.limits.clone())
            .unwrap_or_default();
        let mut full = Monitor::new(self.ruleset.clone(), limits.clone())?;
        for step in &self.trace {
            full.apply_step(step)?;
        }
        let kernel_verdict = full.verdict();
        if kernel_verdict != full_report.verdict {
            failures.push(CheckFailure {
                check: "kernel_vs_oracle_verdict",
                detail: format!(
                    "fresh kernel {} != oracle {}",
                    kernel_verdict.as_str(),
                    full_report.verdict.as_str()
                ),
            });
        }

        // 3) cut point handling (or end of trace when no cut exists).
        let cut = match self.snapshot_after_index {
            None => self.trace.len(),
            Some(c) => c as usize,
        };
        if cut > self.trace.len() {
            return Err(AppError::input("bad_evidence", "snapshot_after_index beyond trace length"));
        }
        if self.snapshot.is_some() != self.snapshot_after_index.is_some() {
            return Err(AppError::input(
                "bad_evidence",
                "snapshot and snapshot_after_index must appear together",
            ));
        }

        // Obligations are compared at the cut against an oracle replay of
        // the prefix only.
        let prefix_report = evaluate(&self.ruleset, &self.trace[..cut])?;
        compare_obligations("obligations_at_cut", &self.online_obligations, &prefix_report, &mut failures);

        let mut restored_verdict = None;
        if let Some(saved) = &self.snapshot {
            let version_mismatch = saved.ruleset_version != self.ruleset.version;
            let hash_mismatch = saved.ruleset_hash != hash;
            if hash_mismatch {
                failures.push(CheckFailure {
                    check: "snapshot_ruleset_hash",
                    detail: "snapshot was produced for different rule-set content".to_string(),
                });
            }
            if version_mismatch {
                failures.push(CheckFailure {
                    check: "snapshot_ruleset_version",
                    detail: format!(
                        "snapshot version `{}` != `{}`",
                        saved.ruleset_version, self.ruleset.version
                    ),
                });
            }

            // Snapshot determinism: a fresh kernel driven to the cut must
            // serialize to exactly the recorded snapshot.  Skipped when the
            // snapshot belongs to another rule set entirely.
            if !version_mismatch && !hash_mismatch {
                let mut to_cut = Monitor::new(self.ruleset.clone(), limits.clone())?;
                for step in &self.trace[..cut] {
                    to_cut.apply_step(step)?;
                }
                if to_cut.snapshot() != *saved {
                    failures.push(CheckFailure {
                        check: "snapshot_determinism",
                        detail: "recorded snapshot differs from a recomputed snapshot at the same cut"
                            .to_string(),
                    });
                }

                // Recovery consistency: restore and re-drive the suffix.
                let restored = match Monitor::restore(saved.clone(), &self.ruleset) {
                    Ok(m) => m,
                    Err(e) => {
                        failures.push(CheckFailure {
                            check: "snapshot_unrestorable",
                            detail: format!("{} [{}]: {}", e.kind.as_str(), e.reason, e.detail),
                        });
                        return Ok(EvidenceReport {
                            valid: false,
                            failures,
                            oracle_verdict: full_report.verdict,
                            kernel_verdict,
                            restored_verdict: None,
                            obligation_count: full_report.obligations.len(),
                        });
                    }
                };
                let mut restored = restored;
                for step in &self.trace[cut..] {
                    restored.apply_step(step)?;
                }
                let rv = restored.verdict();
                restored_verdict = Some(rv);
                if rv != full_report.verdict {
                    failures.push(CheckFailure {
                        check: "restore_vs_oracle_verdict",
                        detail: format!(
                            "restored run {} != oracle {}",
                            rv.as_str(),
                            full_report.verdict.as_str()
                        ),
                    });
                }
                if rv != self.claimed_verdict {
                    failures.push(CheckFailure {
                        check: "restore_vs_claimed",
                        detail: format!(
                            "restored run {} != claimed {}",
                            rv.as_str(),
                            self.claimed_verdict.as_str()
                        ),
                    });
                }
            }
        } else if self.online_obligations.len() != full_report.obligations.len() {
            // Without a cut, obligations describe the end of the trace.
            compare_obligations("obligations_at_end", &self.online_obligations, &full_report, &mut failures);
        }

        Ok(EvidenceReport {
            valid: failures.is_empty(),
            failures,
            oracle_verdict: full_report.verdict,
            kernel_verdict,
            restored_verdict,
            obligation_count: full_report.obligations.len(),
        })
    }
}
