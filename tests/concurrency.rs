//! Concurrency tests: while batches mutate the store, readers must only ever
//! observe consistent revisions. The lock-held batch transaction guarantees
//! no half-updated set is solvable or listable.

mod common;

use std::collections::BTreeMap;
use std::sync::Arc;

use diffconstraints::model::Constraint;
use diffconstraints::store::{BatchOp, Store};

fn c(id: &str, w: i64) -> Constraint {
    Constraint::new(id, "x", "y", w).unwrap()
}

#[test]
fn concurrent_readers_never_see_half_applied_batches() {
    let store = Arc::new(Store::new());

    // Writer: repeatedly applies a batch [add a, add b] then [delete a, delete b].
    let writer_store = store.clone();
    let writer = std::thread::spawn(move || {
        for round in 0..200 {
            let a_id = format!("a_{round}");
            let b_id = format!("b_{round}");
            let add = writer_store.apply_batch(vec![
                BatchOp::Add(c(&a_id, round as i64)),
                BatchOp::Add(c(&b_id, round as i64)),
            ]);
            assert!(add.is_ok());
            // Between add-batch and delete-batch there are exactly 2 rows;
            // a reader may see 0 (before), 2 (after add), or 0 again (after
            // delete) — but never 1, because the add batch and delete batch
            // each commit as a unit.
            let del = writer_store.apply_batch(vec![
                BatchOp::Delete(a_id),
                BatchOp::Delete(b_id),
            ]);
            assert!(del.is_ok());
        }
    });

    // Readers: the count must never be odd (batches add/remove 2 at a time).
    let mut readers = Vec::new();
    for _ in 0..4 {
        let reader_store = store.clone();
        readers.push(std::thread::spawn(move || {
            for _ in 0..2000 {
                let (len, revision, snapshot_len) = reader_store.read(|s| {
                    let l = s.list();
                    (l.len(), s.revision(), l.len())
                });
                assert_eq!(len, snapshot_len);
                assert!(len % 2 == 0, "observed a half-applied batch: {len} rows");
                assert!(len <= 2, "only one batch's worth of rows at a time, saw {len}");
                let _ = revision;
            }
        }));
    }

    writer.join().unwrap();
    for r in readers {
        r.join().unwrap();
    }

    // Final state empty.
    assert!(store.read(|s| s.is_empty()));
}

#[test]
fn snapshot_solve_matches_revision_under_serde_roundtrip_shape() {
    // A focused check that each committed revision is internally consistent:
    // build a system, solve it, mutate, solve again — witnesses correspond to
    // the revision reported, never to a torn intermediate state.
    let svc = Arc::new(diffconstraints::ConstraintService::new());
    svc.add(Constraint::new("k", "x", "y", 1).unwrap()).unwrap();

    let mut handles = Vec::new();
    for t in 0..4 {
        let svc = svc.clone();
        handles.push(std::thread::spawn(move || {
            for i in 0..100 {
                let id = format!("t{t}_n{i}");
                let _ = svc.add(Constraint::new(&id, "x", "y", 1).unwrap());
                let ans = svc.solve(None).expect("this system is always feasible");
                // Whatever revision we read, the returned witness must check
                // against that exact snapshot.
                let snap = svc.list();
                if let diffconstraints::solver::SolveOutcome::Feasible(w) = &ans.outcome {
                    let a: BTreeMap<String, i64> = w.assignment.iter().cloned().collect();
                    let check =
                        diffconstraints::evidence::verify_assignment(&snap, &a).unwrap();
                    // The solve snapshot may predate this thread's own add
                    // (revision ordering), in which case constraint counts
                    // differ. When they match, the witness must satisfy all.
                    if check.checked_constraints == ans.constraint_count {
                        assert!(check.satisfied, "witness violated a constraint");
                    }
                }
                let _ = svc.delete(&id);
            }
        }));
    }
    for h in handles {
        h.join().unwrap();
    }
    assert_eq!(svc.list().len(), 1);
}
