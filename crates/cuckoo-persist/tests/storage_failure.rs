//! 存储故障注入：当原子落盘失败时，内存状态必须完整回滚，
//! 且故障排除后 flush 能恢复、快照可重新打开。
//!
//! 故障手法：服务首次创建会成功写出快照文件；随后把该**文件路径替换为同名目录**，
//! 于是 rename(临时文件 -> 目录) 在 Linux 上返回 EISDIR，稳定触发存储错误。

use cuckoo_core::{FilterParams, FailureKind};
use cuckoo_persist::{PersistError, ServiceState};
use tempfile::TempDir;

const KEY: &[u8] = b"storage-fail-key";

fn is_storage_error(e: &PersistError) -> bool {
    matches!(e, PersistError::Storage(_))
}

fn turn_leaf_into_dir(path: &std::path::Path) {
    assert!(path.is_file(), "前置：快照应是普通文件");
    std::fs::remove_file(path).unwrap();
    std::fs::create_dir(path).unwrap();
}

#[test]
fn failed_insert_flush_rolls_back_memory() {
    let dir = TempDir::new().unwrap();
    let snap = dir.path().join("snapshot.bin");
    let p = FilterParams::new(6, 4, 8, 100).unwrap();
    let mut svc =
        ServiceState::create(&snap, p, b"master-key-0123456789abcdef".to_vec(), 1).unwrap();

    let before_occ = svc.stats().occupied_slots;
    let before_copies = svc.lookup(KEY).observed_copies;

    // 制造落盘失败。
    turn_leaf_into_dir(&snap);

    let err = svc.insert(KEY);
    assert!(err.as_ref().is_err_and(|e| is_storage_error(e)), "期望存储错误: {err:?}");

    // 内存回滚：占用计数不变；新副本不出现；错误不能是成功。
    assert_eq!(svc.stats().occupied_slots, before_occ);
    assert_eq!(svc.lookup(KEY).observed_copies, before_copies);
    assert!(!svc.lookup(KEY).member);

    // 排除故障：删除同名目录，让路径重新可写。
    std::fs::remove_dir(&snap).unwrap();
    svc.flush().expect("故障排除后应能成功落盘");

    // 快照可被同参数重新打开，且不含回滚掉的那次插入。
    let reopened =
        ServiceState::open(&snap, p, b"master-key-0123456789abcdef".to_vec(), 1).unwrap();
    assert!(!reopened.lookup(KEY).member);
    assert_eq!(reopened.stats().occupied_slots, before_occ);
}

#[test]
fn failed_delete_flush_rolls_back_memory_and_credential_still_valid() {
    let dir = TempDir::new().unwrap();
    let snap = dir.path().join("snapshot.bin");
    let p = FilterParams::new(6, 4, 8, 100).unwrap();
    let mut svc =
        ServiceState::create(&snap, p, b"master-key-0123456789abcdef".to_vec(), 1).unwrap();

    let ins = svc.insert(KEY).unwrap();
    let occ_after_insert = svc.stats().occupied_slots;
    assert_eq!(occ_after_insert, 1);
    assert!(svc.lookup(KEY).member);

    // 制造落盘失败后删除。
    turn_leaf_into_dir(&snap);
    let err = svc.delete(KEY, &ins.token);
    assert!(err.as_ref().is_err_and(|e| is_storage_error(e)), "期望存储错误: {err:?}");

    // 内存回滚：键仍在、占用计数不变、凭证仍可再用（未被消费）。
    assert!(svc.lookup(KEY).member);
    assert_eq!(svc.stats().occupied_slots, 1);

    // 排除故障后，用同一张凭证删除必须成功（证明故障没有把凭证提前作废）。
    std::fs::remove_dir(&snap).unwrap();
    let rem = svc.delete(KEY, &ins.token).expect("恢复后凭证应仍有效");
    assert_eq!(rem.load_factor, 0.0);
    assert!(!svc.lookup(KEY).member);

    // 凭证此刻才真正消费：重放得到 CREDENTIAL_EXHAUSTED。
    let replay = svc.delete(KEY, &ins.token).unwrap_err();
    assert_eq!(
        match replay {
            PersistError::Core(c) => c.kind(),
            _ => panic!("非核心错误"),
        },
        FailureKind::CredentialExhausted
    );
}
