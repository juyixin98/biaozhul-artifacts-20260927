//! Randomized differential testing kernel vs independent brute-force oracle.
//!
//! A fixed-seed deterministic PRNG synthesizes small LTS pairs with hidden
//! internal steps and explicit accepting sets. Every generated pair is checked
//! by both implementations; verdicts and shortest counterexample words must
//! agree. The reference answers come from the oracle module, which never calls
//! into the solver.

use rand::rngs::StdRng;
use rand::{Rng, SeedableRng};
use wtio::compiler;
use wtio::input::{CheckRequest, LimitsDef, LtsDef, TransitionDef};
use wtio::oracle;
use wtio::solver::{self, Verdict};

struct Gen {
    rng: StdRng,
}

impl Gen {
    fn new(seed: u64) -> Self {
        Self {
            rng: StdRng::seed_from_u64(seed),
        }
    }

    fn lts(&mut self, name: &str, states: usize, alphabet: &[String], silent: &str) -> LtsDef {
        let state_names: Vec<String> = (0..states).map(|i| format!("{name}{i}")).collect();
        let mut transitions = Vec::new();
        // Sparse random graph: ~1.5 outgoing edges per state, mixing tau in.
        for s in 0..states {
            let edges = self.rng.gen_range(0..=3);
            for _ in 0..edges {
                let target = self.rng.gen_range(0..states);
                let action = if self.rng.gen_bool(0.35) {
                    silent.to_string()
                } else {
                    alphabet[self.rng.gen_range(0..alphabet.len())].clone()
                };
                transitions.push(TransitionDef {
                    from: state_names[s].clone(),
                    action,
                    to: state_names[target].clone(),
                });
            }
        }
        // Random accepting set (non-empty with 70% probability).
        let accepting: Vec<String> = state_names
            .iter()
            .filter(|_| self.rng.gen_bool(0.4))
            .cloned()
            .collect();
        let accepting = if accepting.is_empty() && self.rng.gen_bool(0.5) {
            Some(state_names.clone())
        } else {
            Some(accepting)
        };
        LtsDef {
            name: name.to_string(),
            initial: state_names[0].clone(),
            states: state_names,
            accepting,
            transitions,
        }
    }
}

fn synthesize(seed: u64) -> CheckRequest {
    let mut g = Gen::new(seed);
    let alpha: Vec<String> = ["a", "b", "c"].iter().map(|s| s.to_string()).collect();
    let spec_states = g.rng.gen_range(2..7);
    let impl_states = g.rng.gen_range(2..7);
    let spec = g.lts("s", spec_states, &alpha, "tau");
    let impl_ = g.lts("i", impl_states, &alpha, "tau");
    CheckRequest {
        silent_action: "tau".to_string(),
        alphabet: Some(alpha),
        specification: spec,
        implementation: impl_,
        limits: Some(LimitsDef::default()),
    }
}

#[test]
fn randomized_kernel_vs_oracle() {
    let depth = 6;
    let mut not_included = 0;
    let mut included = 0;
    for seed in 1..=300 {
        let req = synthesize(seed);
        let pair = compiler::compile(&req).unwrap_or_else(|e| panic!("seed {seed}: {e}"));
        let outcome = solver::check(&pair, &req.limits()).expect("check");
        let o = oracle::brute_force_diff(&pair, depth);

        match outcome.verdict {
            Verdict::Included => {
                assert!(
                    !o.impl_yes_spec_no,
                    "seed {seed}: kernel=included, oracle found impl-only trace {:?}",
                    o.shortest_diff
                );
                included += 1;
            }
            Verdict::NotIncluded => {
                let ce = outcome.counterexample.unwrap();
                assert!(
                    o.impl_yes_spec_no,
                    "seed {seed}: kernel found {:?} but oracle sees no impl-only divergence at depth {depth}",
                    ce.trace
                );
                let ow: Vec<String> = o
                    .shortest_diff
                    .iter()
                    .map(|l| pair.label_name(*l).to_string())
                    .collect();
                assert_eq!(ow, ce.trace, "seed {seed}: shortest word mismatch");
                not_included += 1;
            }
            Verdict::Unknown => {}
        }
    }
    // Sanity: the random family must actually exercise both outcomes,
    // otherwise this test would vacuously pass.
    assert!(included > 50, "too few included samples: {included}");
    assert!(not_included > 20, "too few not-included samples: {not_included}");
}
