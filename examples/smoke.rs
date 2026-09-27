use mphf::{build, format, BuildConfig, Probe, VerifyMode};

fn main() {
    let keys: Vec<Vec<u8>> = (0..1000).map(|i| format!("item-{i:05}").into_bytes()).collect();
    let cfg = BuildConfig {
        verify: VerifyMode::FullKey,
        ..Default::default()
    };
    let rep = build(keys.clone(), &cfg).unwrap();
    println!("n={} m={} seed={} attempts={}", rep.index.key_count(), rep.index.vertex_count(), rep.seed, rep.attempts);
    for k in &keys {
        match rep.index.probe(k) {
            Probe::Member { .. } => {}
            other => panic!("member rejected: {other:?}"),
        }
    }
    // permutation check
    let mut slots: std::collections::HashSet<u64> = std::collections::HashSet::new();
    for k in &keys {
        if let Probe::Member { slot } = rep.index.probe(k) { assert!(slots.insert(slot)); }
    }
    assert_eq!(slots.len(), keys.len());
    // non-members
    for i in 1000..1100 {
        let k = format!("item-{i:05}");
        match rep.index.probe(k.as_bytes()) {
            Probe::Rejected{..} => {}
            other => panic!("non-member {k}: {other:?}"),
        }
    }
    format::save_to_path("/tmp/smoke.mphf", &rep.index).unwrap();
    let idx2 = format::load_from_path("/tmp/smoke.mphf").unwrap();
    assert!(matches!(idx2.probe(b"item-00000"), Probe::Member { .. }));
    assert!(matches!(idx2.probe(b"item-00999"), Probe::Member { .. }));
    assert!(matches!(idx2.probe(b"nope"), Probe::Rejected { .. }));
    // empty set
    let empty = build(vec![], &cfg).unwrap();
    assert!(matches!(empty.index.probe(b"anything"), Probe::Rejected { .. }));
    format::save_to_path("/tmp/empty.mphf", &empty.index).unwrap();
    let e2 = format::load_from_path("/tmp/empty.mphf").unwrap();
    assert!(matches!(e2.probe(b"x"), Probe::Rejected { .. }));
    println!("SMOKE OK");
}
