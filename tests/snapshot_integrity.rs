//! 快照完整性测试：任何篡改/截断都必须被 CRC 或 SHA-256 检出并拒绝加载，
//! 绝不允许带着损坏状态启动；配置参数不匹配也必须拒绝。

use deletable_cuckoo::filter::CuckooFilter;
use deletable_cuckoo::hashing::KernelParams;
use deletable_cuckoo::ledger::Ledger;
use deletable_cuckoo::persistence::{decode_snapshot, encode_snapshot, Counters};
use deletable_cuckoo::service::{Service, ServiceError};
use deletable_cuckoo::{KERNEL_VERSION, SNAPSHOT_FORMAT_VERSION, VERSION};
use tempfile::TempDir;

fn params() -> KernelParams {
    KernelParams {
        version: KERNEL_VERSION,
        num_buckets: 32,
        bucket_size: 4,
        fingerprint_bits: 12,
        max_kicks: 100,
        seed: [0x33u8; 32],
    }
}

fn populated() -> (KernelParams, CuckooFilter, Ledger, Counters) {
    let p = params();
    let mut f = CuckooFilter::new(p.clone());
    let mut l = Ledger::new();
    for n in 0..20u64 {
        let k = format!("snap-{n}").into_bytes();
        let id = deletable_cuckoo::token::key_id(&k);
        f.insert(&k, id);
        l.record_issue(id);
    }
    let c = Counters {
        insert_ok: 20,
        ..Default::default()
    };
    (p, f, l, c)
}

#[test]
fn valid_snapshot_roundtrips() {
    let (p, f, l, c) = populated();
    let bytes = encode_snapshot(&p, &f, &l, &c).unwrap();
    let loaded = decode_snapshot(&bytes, &p).expect("合法快照应通过");
    assert_eq!(loaded.filter.total_copies(), f.total_copies());
    assert_eq!(loaded.ledger, l);
}

#[test]
fn truncated_snapshot_is_rejected() {
    let (p, f, l, c) = populated();
    let bytes = encode_snapshot(&p, &f, &l, &c).unwrap();
    let cut = bytes.len() - 50; // 砍掉尾部（含 SHA）
    let err = match decode_snapshot(&bytes[..cut], &p) {
        Err(e) => e,
        Ok(_) => panic!("截断快照不应加载成功"),
    };
    assert!(matches!(
        err,
        deletable_cuckoo::persistence::PersistError::Corrupt(_)
    ));
}

#[test]
fn cell_region_tamper_is_detected_by_sha256() {
    let (p, f, l, c) = populated();
    let mut bytes = encode_snapshot(&p, &f, &l, &c).unwrap();
    // 在单元区翻转一个字节（位于 ledger 之后）。
    let probe = bytes.len() - 40;
    bytes[probe] ^= 0x80;
    let err = decode_snapshot(&bytes, &p).unwrap_err();
    match err {
        deletable_cuckoo::persistence::PersistError::Corrupt(msg) => {
            assert!(msg.contains("SHA-256") || msg.contains("损坏") || msg.contains("不一致"));
        }
        other => panic!("应为 Corrupt，实际 {other:?}"),
    }
}

#[test]
fn header_param_tamper_is_detected_by_crc() {
    let (p, f, l, c) = populated();
    let mut bytes = encode_snapshot(&p, &f, &l, &c).unwrap();
    // 篡改 max_kicks（偏移 32）但不更新 CRC。
    bytes[32] ^= 0x01;
    let err = decode_snapshot(&bytes, &p).unwrap_err();
    match err {
        deletable_cuckoo::persistence::PersistError::Corrupt(msg) => {
            assert!(msg.contains("CRC"), "应被头部 CRC 检出，实际：{msg}");
        }
        other => panic!("应为 Corrupt，实际 {other:?}"),
    }
}

#[test]
fn config_mismatch_is_rejected_even_if_checksum_fixed() {
    // 改了参数又重算校验和的「精巧篡改」：必须因参数不等于当前配置而被拒绝。
    let (mut p, f, l, c) = populated();
    let bytes = encode_snapshot(&p, &f, &l, &c).unwrap();
    // 当前配置不同（桶数翻倍），即使 CRC/SHA 都合法（直接解码原 bytes）也不允许加载。
    p.num_buckets = 64;
    let err = decode_snapshot(&bytes, &p).unwrap_err();
    match err {
        deletable_cuckoo::persistence::PersistError::Corrupt(msg) => {
            assert!(msg.contains("参数") || msg.contains("不自洽") || msg.contains("不一致"));
        }
        other => panic!("应拒绝参数不匹配，实际 {other:?}"),
    }
}

#[test]
fn service_refuses_to_start_on_corrupt_snapshot() {
    let dir = TempDir::new().unwrap();
    let p = params();
    // 先写一个合法快照。
    {
        let svc = Service::open(
            p.clone(),
            dir.path(),
            "snap.bin",
            "",
            false,
            "r".into(),
            VERSION.into(),
            KERNEL_VERSION,
            SNAPSHOT_FORMAT_VERSION,
        )
        .unwrap();
        svc.insert(b"boot-x", None).unwrap();
    }
    // 破坏文件尾部。
    let path = dir.path().join("snap.bin");
    let mut bytes = std::fs::read(&path).unwrap();
    let last = bytes.len() - 1;
    bytes[last] ^= 0xff;
    std::fs::write(&path, bytes).unwrap();

    let err = Service::open(
        p,
        dir.path(),
        "snap.bin",
        "",
        false,
        "r2".into(),
        VERSION.into(),
        KERNEL_VERSION,
        SNAPSHOT_FORMAT_VERSION,
    )
    .unwrap_err();
    assert!(
        matches!(err, ServiceError::SnapshotCorrupt(_)),
        "损坏快照必须阻止启动，实际 {err}"
    );
}
