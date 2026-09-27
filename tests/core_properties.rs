//! 核心正确性测试（不经过 HTTP，直接驱动过滤器/记账内核）。
//!
//! 覆盖验收要点：
//! * 小桶强制迁移环 + 失败插入**逐字节回滚**；
//! * 容量耗尽返回明确类别 `FilterFull`，不是静默成功；
//! * 重复插入计数语义（copies 精确、槽位不增加）；
//! * 全部存活键零假阴性；
//! * **跨 owner 指纹碰撞**时删除一个键绝不影响其他键；
//! * 确定性迁移种子可复现。

use deletable_cuckoo::filter::{CuckooFilter, InsertOutcome};
use deletable_cuckoo::hashing::{fingerprint, KernelParams};
use deletable_cuckoo::ledger::Ledger;
use deletable_cuckoo::token::key_id;

fn params(m: u64, b: u32, f: u32, kicks: u32) -> KernelParams {
    KernelParams {
        version: 1,
        num_buckets: m,
        bucket_size: b,
        fingerprint_bits: f,
        max_kicks: kicks,
        seed: [0x42u8; 32],
    }
}

/// 找到两个「指纹相同」但键不同的输入（仅用于构造碰撞夹具；不产生被测期望值）。
fn find_fingerprint_collision(f_bits: u32, seed: [u8; 32]) -> (Vec<u8>, Vec<u8>) {
    let mut groups: std::collections::HashMap<u32, Vec<u8>> = std::collections::HashMap::new();
    for n in 0..200_000u64 {
        let key = format!("collide-{n}").into_bytes();
        let fp = fingerprint(&key, &seed, f_bits);
        if let Some(other) = groups.get(&fp) {
            // 必须确实不同键。
            if other != &key {
                return (other.clone(), key);
            }
        }
        groups.entry(fp).or_insert(key);
    }
    panic!("在扫描范围内未找到指纹碰撞（f={f_bits}）");
}

#[test]
fn failed_insert_rolls_back_byte_exact_under_forced_kick_ring() {
    // 4 桶 x 2 槽 = 8 槽的极小表；max_kicks 很小，极易触发迁移环。
    let p = params(4, 2, 8, 8);
    let mut f = CuckooFilter::new(p.clone());

    // 填满到容量耗尽；每次失败都必须回滚。
    let mut inserted: Vec<Vec<u8>> = Vec::new();
    let mut saw_kicks = 0u32;
    let mut full_failures = 0;
    for n in 0..500u64 {
        let key = format!("ring-{n}").into_bytes();
        let before = f.cells().to_vec();
        let occ_before = f.occupied_slots();
        match f.insert_seeded(&key, key_id(&key), n.wrapping_mul(2654435761) >> 8) {
            InsertOutcome::Inserted { kicks } => {
                assert_eq!(f.occupied_slots(), occ_before + 1, "成功插入应多占一个槽");
                inserted.push(key);
                saw_kicks = saw_kicks.max(kicks);
            }
            InsertOutcome::FilterFull { attempted_kicks } => {
                full_failures += 1;
                assert_eq!(attempted_kicks, p.max_kicks, "必须恰好迁移到上限");
                // 关键断言：失败后逐字节回到调用前。
                assert_eq!(
                    f.cells(),
                    before.as_slice(),
                    "容量耗尽后未逐字节回滚（第 {n} 次插入）"
                );
                assert_eq!(f.occupied_slots(), occ_before);
            }
            other => panic!("意外结果 {other:?}"),
        }
    }

    assert!(full_failures > 0, "极小表必须实际触发容量耗尽");
    assert!(f.occupied_slots() <= 8, "占用不可能超过槽总数");
    assert!(saw_kicks >= 1, "必须实际发生过迁移（否则没有测到迁移环）");

    // 所有已报告插入成功的键仍可查到（零假阴性）。
    for k in &inserted {
        assert!(f.contains(k), "已成功插入的键 {k:?} 却查不到（假阴性）");
    }
}

#[test]
fn rollback_is_exact_even_with_high_kick_budget() {
    // 更宽的迁移上限：回滚正确性不能依赖「只迁移了几次」。
    let p = params(8, 2, 8, 64);
    let mut f = CuckooFilter::new(p.clone());
    for n in 0..200u64 {
        let key = format!("h-{n}").into_bytes();
        let before = f.cells().to_vec();
        if let InsertOutcome::FilterFull { .. } =
            f.insert_seeded(&key, key_id(&key), 0x9e3779b97f4a7c15 ^ n)
        {
            assert_eq!(f.cells(), before.as_slice(), "高上限迁移回滚不精确");
        }
    }
}

#[test]
fn duplicate_insert_counts_copies_without_new_slot_and_deletes_partially() {
    let p = params(16, 4, 12, 50);
    let mut f = CuckooFilter::new(p.clone());
    let key = b"repeat-key";
    let owner = key_id(key);

    let r1 = f.insert(key, owner);
    assert!(matches!(r1, InsertOutcome::Inserted { kicks: 0 }));
    assert_eq!(f.occupied_slots(), 1);
    assert_eq!(f.total_copies(), 1);

    // 再插两次相同键：都是 Duplicate，槽位仍为 1，副本数为 3。
    assert_eq!(f.insert(key, owner), InsertOutcome::Duplicate);
    assert_eq!(f.insert(key, owner), InsertOutcome::Duplicate);
    assert_eq!(f.occupied_slots(), 1, "重复键不得占用新槽");
    assert_eq!(f.total_copies(), 3);
    // 该单元的 copies 必须确为 3。
    let cell = f
        .cells()
        .iter()
        .find(|c| c.owner == owner && c.fingerprint != 0)
        .expect("应存在该 owner 的单元");
    assert_eq!(cell.copies, 3);

    let pl = deletable_cuckoo::hashing::place(key, &p);
    // 删除一份：copies 3->2，槽仍占用，成员仍为真。
    let d = f.decrement(owner, pl.i1, pl.i2, pl.fingerprint).unwrap();
    assert_eq!(d.copies_before, 3);
    assert_eq!(f.occupied_slots(), 1);
    assert_eq!(f.total_copies(), 2);
    assert!(f.contains(key));

    // 删到 0：槽位清空。
    f.decrement(owner, pl.i1, pl.i2, pl.fingerprint).unwrap();
    let last = f.decrement(owner, pl.i1, pl.i2, pl.fingerprint).unwrap();
    assert_eq!(last.copies_before, 1);
    assert_eq!(f.occupied_slots(), 0);
    assert_eq!(f.total_copies(), 0);

    // 再删（owner 已无槽）必须是明确错误，而不是假成功。
    let err = f
        .decrement(owner, pl.i1, pl.i2, pl.fingerprint)
        .unwrap_err();
    assert!(matches!(
        err,
        deletable_cuckoo::filter::FilterError::Invariant(_)
    ));
}

#[test]
fn deleting_one_key_never_removes_a_colliding_other_owners_slot() {
    let seed = [0x42u8; 32];
    let (ka, kb) = find_fingerprint_collision(4, seed);
    // 健全性：二者确实指纹相同、owner 不同。
    assert_eq!(fingerprint(&ka, &seed, 4), fingerprint(&kb, &seed, 4));
    assert_ne!(key_id(&ka), key_id(&kb));

    let p = params(64, 4, 4, 100);
    let mut f = CuckooFilter::new(p.clone());
    assert!(matches!(
        f.insert(&ka, key_id(&ka)),
        InsertOutcome::Inserted { .. }
    ));
    assert!(matches!(
        f.insert(&kb, key_id(&kb)),
        InsertOutcome::Inserted { .. }
    ));
    // 两个不同 owner 各占一个槽。
    assert_eq!(f.occupied_slots(), 2);
    assert!(f.contains(&ka));
    assert!(f.contains(&kb));

    let pa = deletable_cuckoo::hashing::place(&ka, &p);
    // 用 A 的凭证删除 A。
    f.decrement(key_id(&ka), pa.i1, pa.i2, pa.fingerprint)
        .unwrap();

    // 关键：B 必须仍然可查，绝不能因为共享指纹被连坐删除。
    assert!(f.contains(&kb), "删除 A 误伤了指纹碰撞的 B（假阴性）");
    assert_eq!(f.total_copies(), 1);

    // 用 B 的 owner 去 A 的定位递减必须失败（凭证-拥有者绑定）。
    let wrong = f.decrement(key_id(&kb), pa.i1, pa.i2, pa.fingerprint);
    // 若 B 的定位恰好等于 A，递减会成功删掉 B 自己（合法）；那种情况下 occupied==0。
    // 但 A 的 owner 槽已清空，所以用 B 的 owner 在 A 定位只可能：找不到（错误）或恰好 B 在那里。
    match wrong {
        Ok(d) => {
            // 若成功，删的必须确实是 B（owner 匹配），之后表为空且 A 的 owner 不存在。
            assert_eq!(d.owner, key_id(&kb));
            assert_eq!(f.occupied_slots(), 0);
        }
        Err(_) => {
            // B 还在另一个桶里。
            assert!(f.contains(&kb));
        }
    }
}

#[test]
fn no_false_negatives_after_mixed_inserts_and_credential_deletes() {
    // 维护一份独立的「存活集合」作为基准（不调用被测内核生成期望）。
    let p = params(256, 4, 12, 200);
    let mut f = CuckooFilter::new(p.clone());
    let mut ledger = Ledger::new();

    // 用简单的确定性 RNG（线性同余）决定插入/删除，避免依赖被测实现的随机性口径。
    let mut state: u64 = 0x1234_5678_9abc_def0;
    let mut rng = || {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        state >> 33
    };

    let mut inserted: Vec<Vec<u8>> = Vec::new();
    let n_ops = 4000u64;
    for op in 0..n_ops {
        let key_no = rng() % 600;
        let key = format!("item-{key_no}").into_bytes();
        let id = key_id(&key);
        let do_delete = rng() % 100 < 35 && ledger.live_count(id) > 0;

        if do_delete {
            // 找到一个未花费序号：这里简化为直接在过滤器递减并把最早存活副本视为删除。
            // 为保持记账一致，标记一个已签发但未花费的序号。
            let st = ledger.keys.get(&id).unwrap();
            let ordinal = (0..st.issued)
                .find(|&o| {
                    let w = (o / 64) as usize;
                    st.spent
                        .get(w)
                        .map_or(true, |x| x & (1u64 << (o % 64)) == 0)
                })
                .unwrap();
            ledger.mark_spent(id, ordinal);
            let pl = deletable_cuckoo::hashing::place(&key, &p);
            f.decrement(id, pl.i1, pl.i2, pl.fingerprint)
                .expect("记账存活则递减必须成功");
        } else {
            match f.insert_seeded(&key, id, rng()) {
                InsertOutcome::Inserted { .. } | InsertOutcome::Duplicate => {
                    ledger.record_issue(id);
                    if !inserted.iter().any(|k| k == &key) {
                        inserted.push(key);
                    }
                }
                InsertOutcome::FilterFull { .. } => {
                    // 满了就不再插这个键；回滚由别的测试严格保证。
                }
                other => panic!("意外 {other:?} op={op}"),
            }
        }
    }

    // 断言：所有按记账仍存活的键，过滤器一定判定为成员（零假阴性）。
    let mut live = 0usize;
    let mut dead = 0usize;
    for k in &inserted {
        let id = key_id(k);
        if ledger.live_count(id) > 0 {
            live += 1;
            assert!(
                f.contains(k),
                "存活键 {:?} 被判非成员（假阴性）",
                String::from_utf8_lossy(k)
            );
        } else {
            dead += 1;
        }
    }
    // 交叉不变式：副本总数一致。
    assert_eq!(
        f.total_copies(),
        ledger.total_live(),
        "桶表副本数与记账不一致"
    );
    assert!(live > 50, "测试应保留足够多存活键，实际 {live}");
    assert!(dead > 5, "测试应实际删除过一些键，实际 {dead}");
}
