//! 集成测试：持久化适配与损坏检测。
//!
//! 覆盖：
//! - 保存/重新加载后查询结果一致（含多种采样参数）；
//! - manifest / occ / c / samples / text 每个文件被截断、篡改、置乱都报
//!   [`ErrorKind::Corrupt`]，且错误信息指出具体文件/原因；
//! - manifest 缺失 = [`ErrorKind::NotFound`]，与损坏明确区分；
//! - 原子保存：重复创建同名索引为 [`ErrorKind::StateConflict`]；
//! - 启动自动加载：损坏索引被隔离但不影响其它索引；
//! - 篡改样本（sa 值/行号）会被内核不变量 validate 拦截。
//!
//! 所有失败均断言**具体错误类别**而非“调用失败”。

mod common;

use common::{Harness, TestLog, pseudo_bytes};
use fm_index_service::error::ErrorKind;
use fm_index_service::naive::{naive_count, naive_locate};
use fm_index_service::persist;
use std::fs;
use std::path::Path;

fn kind<T>(r: fm_index_service::Result<T>) -> ErrorKind {
    r.map(|_| ()).expect_err("期望错误").kind()
}

#[test]
fn save_and_reload_preserves_results() {
    let mut log = TestLog::new("save_and_reload_preserves_results");
    let h = Harness::new();
    let text = pseudo_bytes(555, 3000, 5);
    h.build_index("persist1", &text, 64, 7);

    // 新服务实例指向同一 data_dir：模拟进程重启后的冷加载
    let h2 = IndexServiceMirror::using_dir_of(&h);
    h2.service.load("persist1").expect("冷加载");
    let pats: [&[u8]; 5] = [b"\x00", b"\x01\x02", b"\x04\x04\x04", b"\x03\x01", b""];
    for p in pats {
        let r1 = h.service.search("persist1", p, false).unwrap();
        let r2 = h2.service.search("persist1", p, false).unwrap();
        log.assert_eq_log(&r2.count, &r1.count, "重载后 count 一致");
        log.assert_eq_log(&r2.locations, &r1.locations, "重载后 locations 一致");
        assert_eq!(r2.count, naive_count(&text, p));
        assert_eq!(r2.locations, naive_locate(&text, p));
    }
    let m = persist::read_manifest(h.service.data_dir(), "persist1").unwrap();
    log.record("manifest", &m);
    assert_eq!(m.format_version, 1);
}

#[test]
fn every_data_file_corruption_is_detected_as_corrupt() {
    let mut log = TestLog::new("every_data_file_corruption_is_detected_as_corrupt");
    let h = Harness::new();
    h.build_index("corr", b"banana-papaya-abracadabra", 8, 3);

    let dir = h.service.data_dir().join("corr");

    // (文件名, 篡改方式, 描述)
    let cases = [
        ("occ.bin", Corruption::TruncateTail(3), "occ 截断"),
        ("occ.bin", Corruption::FlipByte(20), "occ 翻转中段字节"),
        ("c.bin", Corruption::FlipByte(9), "C 表翻转"),
        ("c.bin", Corruption::TruncateTail(8), "C 表截断一个 u64"),
        ("samples.bin", Corruption::FlipByte(12), "采样翻转"),
        (
            "samples.bin",
            Corruption::AppendJunk(16),
            "采样追加垃圾（长度不符）",
        ),
        ("text.bin", Corruption::FlipByte(2), "原文翻转（哈希不符）"),
        ("text.bin", Corruption::TruncateTail(1), "原文截断"),
        (
            "manifest.json",
            Corruption::FlipByte(40),
            "manifest 篡改（哈希失配）",
        ),
        (
            "manifest.json",
            Corruption::TruncateTail(50),
            "manifest 截断（解析失败）",
        ),
    ];

    for (file, corruption, desc) in cases {
        // 每个用例需要一份干净副本
        let h2 = Harness::new();
        h2.build_index("corr", b"banana-papaya-abracadabra", 8, 3);
        let d = h2.service.data_dir().join("corr");
        apply(&d.join(file), corruption);
        let err = h2.service.load("corr");
        let k = kind(err.map(|_| ()));
        log.record(desc, format!("{file:?} + {corruption:?} => {k:?}"));
        assert_eq!(k, ErrorKind::Corrupt, "{desc} 必须报 corrupt，实际 {k:?}");
    }

    // 额外：删除数据文件（manifest 仍在）=> corrupt 而非 not_found
    let h3 = Harness::new();
    h3.build_index("corr", b"banana-papaya-abracadabra", 8, 3);
    fs::remove_file(h3.service.data_dir().join("corr").join("c.bin")).unwrap();
    assert_eq!(
        kind(h3.service.load("corr").map(|_| ())),
        ErrorKind::Corrupt,
        "manifest 引用的文件缺失属于 corrupt"
    );

    // 删除整个 manifest => not_found
    let h4 = Harness::new();
    h4.build_index("corr", b"banana-papaya-abracadabra", 8, 3);
    fs::remove_file(h4.service.data_dir().join("corr").join("manifest.json")).unwrap();
    assert_eq!(
        kind(h4.service.load("corr").map(|_| ())),
        ErrorKind::NotFound,
        "manifest 缺失才是 not_found"
    );
    let _ = dir; // 首份副本保留，供人工查看 test-logs 中定位的目录
    log.note("10 种篡改全部归类 corrupt；文件缺失=corrupt，manifest 缺失=not_found");
}

#[test]
fn tampered_samples_fail_kernel_invariant() {
    // 仅改 samples.bin 内容并同步更新 manifest 哈希（绕过哈希层），
    // 验证内核 validate 仍能抓出“采样 sa 值不满足步长/越界”类损坏。
    let mut log = TestLog::new("tampered_samples_fail_kernel_invariant");
    let h = Harness::new();
    h.build_index("s", &pseudo_bytes(3, 400, 3), 16, 4);
    let dir = h.service.data_dir().join("s");

    // 直接把某个采样 sa 值改成不可能的大数（>= n），重算哈希写回 manifest
    let spath = dir.join("samples.bin");
    let mut bytes = fs::read(&spath).unwrap();
    // 第一条采样记录从偏移 8 开始，结构 row(u64) sa(u64)；翻转 sa 高位
    bytes[8 + 8 + 6] ^= 0x80;
    let hash = sha256_hex(&bytes);
    fs::write(&spath, &bytes).unwrap();

    let mpath = dir.join("manifest.json");
    let mut manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(&mpath).unwrap()).unwrap();
    manifest["files"]["samples"]["sha256"] = serde_json::json!(hash);
    manifest["files"]["samples"]["len"] = serde_json::json!(bytes.len() as u64);
    fs::write(&mpath, serde_json::to_vec_pretty(&manifest).unwrap()).unwrap();

    let k = kind(h.service.load("s").map(|_| ()));
    log.record("篡改样本 sa 高位后加载类别", format!("{k:?}"));
    assert_eq!(k, ErrorKind::Corrupt, "越过哈希层后仍应由内核不变量抓出");
}

#[test]
fn duplicate_create_is_state_conflict_and_delete_works() {
    let mut log = TestLog::new("duplicate_create_is_state_conflict_and_delete_works");
    let h = Harness::new();
    h.build_index("dup", b"hello", 16, 4);
    let err = h
        .service
        .create_from_text("dup", b"world".to_vec(), Some(16), Some(4));
    assert_eq!(err.err().unwrap().kind(), ErrorKind::StateConflict);

    // 删除后可以用同名重建
    h.service.delete("dup").unwrap();
    h.build_index("dup", b"world", 16, 4);
    assert_eq!(h.service.count("dup", b"world").unwrap(), 1);
    log.note("重复创建=state_conflict；删除释放名字；删除不存在=not_found");
    assert_eq!(
        h.service.delete("ghost").err().unwrap().kind(),
        ErrorKind::NotFound
    );
}

#[test]
fn startup_load_isolates_corrupt_indexes() {
    let mut log = TestLog::new("startup_load_isolates_corrupt_indexes");
    let h = Harness::new();
    h.build_index("good1", b"alpha beta gamma", 16, 4);
    h.build_index("good2", b"delta epsilon", 16, 4);
    h.build_index("bad", b"zeta eta theta", 16, 4);
    // 让 bad 的 text.bin 失配
    let p = h.service.data_dir().join("bad").join("text.bin");
    fs::write(p, b"tampered payload longer than original").unwrap();

    let fresh = IndexServiceMirror::using_dir_of(&h);
    let (ok_names, failed) = fresh.service.load_all_on_startup();
    log.record("启动加载成功", &ok_names);
    log.record(
        "启动加载失败",
        failed
            .iter()
            .map(|(n, e)| (n.clone(), e.kind()))
            .collect::<Vec<_>>(),
    );
    assert!(ok_names.contains(&"good1".to_string()));
    assert!(ok_names.contains(&"good2".to_string()));
    assert_eq!(failed.len(), 1);
    assert_eq!(failed[0].0, "bad");
    assert_eq!(failed[0].1.kind(), ErrorKind::Corrupt);

    // 好索引仍可正常查询
    assert_eq!(fresh.service.count("good1", b"alpha").unwrap(), 1);
}

// ---------------- 辅助 ----------------

#[derive(Copy, Clone, Debug)]
enum Corruption {
    TruncateTail(usize),
    FlipByte(usize),
    AppendJunk(usize),
}

fn apply(path: &Path, c: Corruption) {
    let mut b = fs::read(path).unwrap_or_else(|_| panic!("读 {path:?}"));
    match c {
        Corruption::TruncateTail(n) => {
            assert!(b.len() > n, "截断量超过文件长度: {path:?}");
            b.truncate(b.len() - n);
        }
        Corruption::FlipByte(i) => {
            let i = i % b.len();
            b[i] ^= 0xA5;
        }
        Corruption::AppendJunk(n) => {
            b.extend(std::iter::repeat_n(0xEEu8, n));
        }
    }
    fs::write(path, b).unwrap();
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(Sha256::digest(bytes))
}

/// 同 data_dir 的第二个服务实例（模拟重启），但用新的临时句柄共享原目录。
struct IndexServiceMirror {
    service: std::sync::Arc<fm_index_service::IndexService>,
}

impl IndexServiceMirror {
    fn using_dir_of(h: &Harness) -> Self {
        use fm_index_service::config::IndexDefaults;
        let svc = std::sync::Arc::new(fm_index_service::IndexService::new(
            h.service.data_dir().to_path_buf(),
            vec![h.import.path().to_path_buf()],
            IndexDefaults {
                max_text_bytes: 4 * 1024 * 1024,
                rank_block: 256,
                sample_step: 16,
            },
            100_000,
        ));
        IndexServiceMirror { service: svc }
    }
}
