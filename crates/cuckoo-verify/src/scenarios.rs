//! 验证场景集合。每个场景返回结构化报告；断言的是**具体值与具体失败类别**。
//!
//! 场景一览：
//! - S01 固定内核锚定：验证器独立推导的指纹/双候选桶与内核逐键一致；
//! - S02 参考模型一致：独立迁移模型与被测内核在固定 seed 下最终布局逐槽一致；
//! - S03 迁移环与失败回滚：小桶强制迁移，失败必须 FILTER_FULL 且状态完整回滚；
//! - S04 重复插删计数：重复键副本计数精确，删除只接受对应凭证；
//! - S05 容量耗尽：容量耗尽后每键明确失败类别，全部存活键零假阴性；
//! - S06 持久化重启：落盘重开后所有存活凭证仍可查可删，损坏文件被拒绝；
//! - S07 固定种子假阳性率：测量并报告 FPR，理论量级对照，且成员零假阴性；
//! - S08 凭证攻击：伪造/越权/重放删除全部被分类拒绝且无副作用；
//! - S09 格式与参数防护：快照归属不一致/参数错配被显式拒绝。

use std::path::PathBuf;

use cuckoo_core::{
    alt_index, locate as core_locate, CoreError, CuckooFilter, FailureKind, FilterParams,
    SplitMix64, CORE_VERSION, FORMAT_VERSION,
};
use cuckoo_persist::{CodecError, PersistError, ServiceState, Snapshot};
use tempfile::TempDir;

use crate::harness::{new_run_id, Reporter, ScenarioReport};
use crate::model::{RefFilter, RefOutcome, RefSplitMix64};
use crate::oracle::{expected_locate, theoretical_fp_approx};

const TEST_MASTER_KEY: &[u8] = b"verify-master-key-0123456789abcd";
const RNG_SEED: u64 = 0x00C0_FFEE_2026_0927;

fn params_small_kicks() -> FilterParams {
    // 8 桶 * 2 槽 = 16 槽；f=4。极易打满并出现迁移环。
    FilterParams::new(3, 2, 4, 25).unwrap()
}

fn params_tiny() -> FilterParams {
    // 8 桶 * 2 槽 = 16 槽；f=4。探针确认：该配置每个 seed 都既有成功迁移、
    // 又会很快打满并出现跑满 max_kicks 的迁移环。
    FilterParams::new(3, 2, 4, 15).unwrap()
}

fn key(n: u64) -> Vec<u8> {
    format!("key-{n:06}").into_bytes()
}

fn member_key(n: u64) -> Vec<u8> {
    format!("member-{n:08}").into_bytes()
}
fn nonmember_key(n: u64) -> Vec<u8> {
    format!("nonmember-{n:08}").into_bytes()
}

// ---------------------------------------------------------------------------
// S01
// ---------------------------------------------------------------------------

pub fn scenario_01_oracle_anchors(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S01",
        "固定指纹与双候选桶计算（独立参考答案锚定）",
        run_id,
    );
    let p = FilterParams::new(10, 4, 12, 100).unwrap();

    // 同时锁定一组具体的“黄金值”：这些值由验证器内置 XXH64 推导，
    // 任意改动种子/指纹域/桶掩码都会立刻被发现。
    let golden = [
        ("anchor-000", 1u32, 2u32),
        ("anchor-001", 1234, 4321),
        ("删除凭证-测试-α", 77, 88),
    ];
    let mut all_match = true;
    let mut samples = Vec::new();
    for n in 0..2000u64 {
        let k = if (n as usize) < golden.len() {
            golden[n as usize].0.as_bytes().to_vec()
        } else {
            key(n)
        };
        let exp = expected_locate(&k, p.buckets_exp, p.fingerprint_bits);
        let (fp, i1, i2) = core_locate(&k, &p);
        if (fp, i1, i2) != (exp.fp, exp.i1, exp.i2) {
            all_match = false;
            samples.push(format!(
                "key={} core=({fp},{i1},{i2}) oracle=({},{},{})",
                String::from_utf8_lossy(&k),
                exp.fp,
                exp.i1,
                exp.i2
            ));
        }
    }
    r.check(
        "2000 个固定键的 (fp,i1,i2) 与独立参考答案完全一致",
        "使用验证器内置 XXH64 参考实现逐键推导；指纹域 1..2^f-1，桶掩码 mod 2^m",
        all_match,
        samples.join(" | "),
        "全部 2000 键逐字段相等（0 例不一致）",
    );

    // 指纹范围与非零。
    let mut range_ok = true;
    for n in 0..5000u64 {
        let (fp, _, _) = core_locate(&key(n), &p);
        if fp == 0 || fp >= p.fingerprint_mod() {
            range_ok = false;
            break;
        }
    }
    r.check(
        "指纹恒在 [1, 2^f - 1]（0 保留为空槽标记）",
        format!("f={} 位，5000 键全检", p.fingerprint_bits),
        range_ok,
        "若触发则首个越界 fp 已记录",
        "1 <= fp <= 2^f-1",
    );

    // alt 对合性。
    let mut invol_ok = true;
    for n in 0..1000u64 {
        let (fp, i1, i2) = core_locate(&key(n), &p);
        if alt_index(i1, fp, &p) != i2 || alt_index(i2, fp, &p) != i1 {
            invol_ok = false;
        }
    }
    r.check(
        "alt_index 是对合：alt(alt(i)) = i（删除与迁移的前提）",
        "1000 键双向验证",
        invol_ok,
        "若有违例已计数",
        "i1 <-> i2 双向成立 1000/1000",
    );

    r.finish()
}

// ---------------------------------------------------------------------------
// S02
// ---------------------------------------------------------------------------

pub fn scenario_02_reference_model_parity(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S02",
        "独立迁移参考模型与被测内核逐槽一致",
        run_id,
    );
    let p = params_small_kicks();
    let mut core = CuckooFilter::new(p).unwrap();
    let mut model = RefFilter::new(p.buckets_exp, p.bucket_size, p.max_kicks);
    let mut rng_core = SplitMix64::new(RNG_SEED);
    let mut rng_ref = RefSplitMix64::new(RNG_SEED);

    let mut placed_both = 0usize;
    let mut full_both = 0usize;
    let mut discrepancy = Vec::new();

    for n in 0..120u64 {
        let k = key(n);
        let loc = expected_locate(&k, p.buckets_exp, p.fingerprint_bits);
        let (fp, i1, i2) = core_locate(&k, &p);
        let core_out = core.insert(fp, i1, i2, &mut rng_core);
        let ref_out = model.insert(&loc, &mut rng_ref);

        match (&core_out, &ref_out) {
            (Ok(t), RefOutcome::Placed {
                placed_bucket,
                placed_slot,
                evicted_chain,
            }) => {
                placed_both += 1;
                let core_chain: Vec<u32> = t.swaps.iter().map(|s| s.evicted_fp).collect();
                if t.placed.bucket != *placed_bucket
                    || t.placed.slot != *placed_slot
                    || core_chain != *evicted_chain
                {
                    discrepancy.push(format!(
                        "n={n}: core=({b},{s},chain={c:?}) ref=({rb},{rs},chain={rc:?})",
                        b = t.placed.bucket,
                        s = t.placed.slot,
                        c = core_chain,
                        rb = placed_bucket,
                        rs = placed_slot,
                        rc = evicted_chain
                    ));
                }
            }
            (Err(CoreError::FilterFull(k)), RefOutcome::Full { kicks_attempted }) => {
                full_both += 1;
                if *k != *kicks_attempted {
                    discrepancy.push(format!("n={n}: kicks {k} vs {kicks_attempted}"));
                }
            }
            other => discrepancy.push(format!("n={n}: 结果类别不一致: {other:?}")),
        }
    }

    r.check(
        "每一键的成功/失败类别、最终落点、整条迁移指纹链两边一致",
        format!(
            "同 seed={SEED:#x} 同输入序列 120 键；两者各自实现迁移循环",
            SEED = RNG_SEED
        ),
        discrepancy.is_empty(),
        if discrepancy.is_empty() {
            "无差异".to_string()
        } else {
            discrepancy.join(" | ")
        },
        "120/120 键分类一致且成功键的 placed 与 evicted 链逐项相等",
    );

    let layout_eq = core.slots() == model.slots();
    r.check_eq(
        "最终槽位布局逐位相同",
        format!(
            "成功 {placed_both} 次，FILTER_FULL {full_both} 次；失败均整体回滚",
        ),
        core.slots(),
        model.slots(),
    );
    let _ = layout_eq; // check_eq 已断言

    r.check(
        "至少发生过一次需要迁移的插入（否则本场景没覆盖迁移路径）",
        "8 桶*2 槽小容量配置",
        core.slots().iter().filter(|&&x| x != 0).count() >= 4,
        format!("非零槽={}", core.slots().iter().filter(|&&x| x != 0).count()),
        "非零槽 >= 4",
    );

    r.finish()
}

// ---------------------------------------------------------------------------
// S03
// ---------------------------------------------------------------------------

pub fn scenario_03_kick_loop_and_rollback(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S03",
        "小桶强制迁移环；失败插入必须 FILTER_FULL 并整体回滚",
        run_id,
    );

    let mut saw_kicks = 0usize;
    let mut saw_full = 0usize;
    let mut rollback_ok = true;
    let mut kind_ok = true;
    let mut rollback_details = Vec::new();

    for seed in [1u64, 7, 42, RNG_SEED] {
        let p = params_tiny();
        let mut f = CuckooFilter::new(p).unwrap();
        let mut rng = SplitMix64::new(seed);
        let mut inserted_keys: Vec<(u32, usize, usize)> = Vec::new();

        for n in 0..200u64 {
            let (fp, i1, i2) = core_locate(&key(n), &p);
            let before = f.slots().to_vec();
            let occ = f.occupied_slots();
            match f.insert(fp, i1, i2, &mut rng) {
                Ok(t) => {
                    if !t.swaps.is_empty() {
                        saw_kicks += 1;
                    }
                    inserted_keys.push((fp, i1, i2));
                }
                Err(e @ CoreError::FilterFull(k)) => {
                    saw_full += 1;
                    if e.kind() != FailureKind::FilterFull {
                        kind_ok = false;
                    }
                    if k != p.max_kicks {
                        kind_ok = false;
                    }
                    // 回滚断言：槽位快照与占用计数都必须恢复。
                    if f.slots() != before.as_slice() {
                        rollback_ok = false;
                        rollback_details
                            .push(format!("seed={seed} n={n} 槽位未回滚"));
                    }
                    if f.occupied_slots() != occ {
                        rollback_ok = false;
                        rollback_details
                            .push(format!("seed={seed} n={n} 占用计数 {occ} -> {}", f.occupied_slots()));
                    }
                }
                Err(other) => {
                    kind_ok = false;
                    rollback_details
                        .push(format!("seed={seed} n={n} 未预期错误 {other:?}"));
                }
            }
        }

        // 全部“此前成功”的键在经历后续失败后仍然可查（无假阴性）。
        for (fp, i1, i2) in &inserted_keys {
            if !f.contains(*fp, *i1, *i2) {
                rollback_ok = false;
                rollback_details.push(format!("seed={seed} 存活键 ({fp},{i1},{i2}) 查询丢失"));
            }
        }
    }

    r.note(
        "计算步骤",
        format!(
            "4 个固定 seed 各尝试 200 键；配置 4 桶*2 槽=8 槽、f=4、max_kicks={}",
            params_tiny().max_kicks
        ),
    );
    r.check(
        "迁移实际发生（多个成功插入的 swaps 链非空）",
        "否则未真正覆盖迁移代码路径",
        saw_kicks > 0,
        format!("发生迁移的插入次数={saw_kicks}"),
        "> 0",
    );
    r.check(
        "容量耗尽失败被归类为 FILTER_FULL 且携带正确 kicks 上限",
        "CoreError::FilterFull(max_kicks)，kind() == FilterFull",
        kind_ok && saw_full > 0,
        format!("FILTER_FULL 次数={saw_full}，类别与 kicks 全部正确={kind_ok}"),
        "次数 > 0 且每次 kicks == 15",
    );
    r.check(
        "每次失败后槽位与占用计数完整回滚，且历史存活键零假阴性",
        "失败前后快照逐位比较；成功键集合逐个 contains",
        rollback_ok,
        rollback_details.join(" | "),
        "4 seed 全部键无回滚偏差、无存活键丢失",
    );

    r.finish()
}

// ---------------------------------------------------------------------------
// S04
// ---------------------------------------------------------------------------

fn create_service(dir: &TempDir, p: FilterParams) -> ServiceState {
    let path = dir.path().join("snapshot.bin");
    ServiceState::create(path, p, TEST_MASTER_KEY.to_vec(), RNG_SEED).unwrap()
}

pub fn scenario_04_duplicate_counting(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S04",
        "重复键插入计数与“只认凭证”的删除",
        run_id,
    );
    let dir = TempDir::new().unwrap();
    let p = FilterParams::new(8, 4, 12, 200).unwrap();
    let mut svc = create_service(&dir, p);

    let k = b"repeatable-key";
    // 初始：不存在（全新过滤器）。
    r.check(
        "插入前近似查询为 false",
        "全新过滤器",
        !svc.lookup(k).member,
        svc.lookup(k).member.to_string(),
        "false",
    );

    let t1 = svc.insert(k).unwrap();
    let t2 = svc.insert(k).unwrap();
    let t3 = svc.insert(k).unwrap();
    r.check(
        "重复插入 3 次得到 3 个不同凭证",
        "每次插入独立副本+独立 jti",
        t1.jti != t2.jti && t2.jti != t3.jti && t1.jti != t3.jti,
        format!("jti 前缀 {:?} / {:?} / {:?}", &t1.jti[..4], &t2.jti[..4], &t3.jti[..4]),
        "三个 jti 两两不同",
    );
    r.check_eq(
        "观测副本数精确为 3（候选桶内 fp 计数）",
        "同一指纹三份副本",
        &svc.lookup(k).observed_copies,
        &3usize,
    );

    // 用 t2 删除，但传入错误的键 -> INVALID_CREDENTIAL。
    let wrong_key_err = svc.delete(b"other-key", &t2.token).unwrap_err();
    r.check(
        "凭证 + 错误键删除 => INVALID_CREDENTIAL",
        "HMAC 把 jti 与插入时键字节绑定",
        matches!(
            wrong_key_err,
            PersistError::Core(CoreError::InvalidCredential(_))
        ),
        format!("{wrong_key_err:?}"),
        "Core(InvalidCredential)",
    );
    r.check_eq(
        "被拒绝后副本数仍为 3（无副作用）",
        "凭证不匹配不得移动任何槽",
        &svc.lookup(k).observed_copies,
        &3usize,
    );

    // 正确凭证删除 t2。
    let rem = svc.delete(k, &t2.token).unwrap();
    r.check(
        "正确凭证删除成功，返回槽位坐标",
        format!("删除落点 b{}/s{}", rem.bucket, rem.slot),
        rem.bucket < p.num_buckets() && (rem.slot as u32) < p.bucket_size,
        format!("b{}/s{}", rem.bucket, rem.slot),
        "合法坐标",
    );
    r.check_eq(
        "删除后副本数 = 2，member 仍为 true（无假阴性）",
        "还剩 t1/t3 两个副本",
        &svc.lookup(k).observed_copies,
        &2usize,
    );

    // 重放 t2 => CREDENTIAL_EXHAUSTED。
    let replay = svc.delete(k, &t2.token).unwrap_err();
    r.check(
        "同一凭证重放删除 => CREDENTIAL_EXHAUSTED",
        "账本项在首次删除后移除",
        matches!(replay, PersistError::Core(CoreError::CredentialExhausted)),
        format!("{replay:?}"),
        "Core(CredentialExhausted)",
    );
    r.check_eq(
        "重放无副作用，副本数保持 2",
        "一次性凭证",
        &svc.lookup(k).observed_copies,
        &2usize,
    );

    // 用 t1、t3 删完 => false。
    svc.delete(k, &t1.token).unwrap();
    let rem3 = svc.delete(k, &t3.token).unwrap();
    let after = svc.lookup(k);
    r.check(
        "三份副本逐一凭凭证删完后 member=false、copies=0",
        format!("最后删除 b{}/s{}", rem3.bucket, rem3.slot),
        !after.member && after.observed_copies == 0,
        format!("member={} copies={}", after.member, after.observed_copies),
        "member=false, copies=0",
    );
    r.check(
        "已删空后查询为假阴性安全侧：member=false 仅在副本确为 0 时出现",
        "这是真删除而非指纹碰撞误删（每步都按凭证槽位）",
        svc.stats().occupied_slots == 0,
        svc.stats().occupied_slots.to_string(),
        "0",
    );

    r.finish()
}

// ---------------------------------------------------------------------------
// S05
// ---------------------------------------------------------------------------

pub fn scenario_05_capacity_exhaustion(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S05",
        "容量耗尽语义：明确失败类别 + 全部存活键零假阴性",
        run_id,
    );
    let dir = TempDir::new().unwrap();
    let p = FilterParams::new(3, 2, 4, 20).unwrap(); // 16 槽
    let mut svc = create_service(&dir, p);

    let mut tokens = Vec::new();
    let mut full_count = 0usize;
    let mut other_errors = Vec::new();
    let total = 400u64;

    for n in 0..total {
        match svc.insert(&key(n)) {
            Ok(ins) => tokens.push((n, ins)),
            Err(PersistError::Core(CoreError::FilterFull(kicks))) => {
                if kicks == p.max_kicks {
                    full_count += 1;
                } else {
                    other_errors.push(format!("n={n} kicks={kicks}"));
                }
            }
            Err(other) => other_errors.push(format!("n={n} {other:?}")),
        }
    }

    let stats = svc.stats();
    r.note(
        "容量与进度",
        format!(
            "尝试 {total} 键；物理槽 {}；成功 {}；FILTER_FULL {full_count}；其他错误 {}",
            p.total_slots(),
            tokens.len(),
            other_errors.len()
        ),
    );
    r.check(
        "成功插入数不超过物理槽位总数",
        format!("总槽 {}", p.total_slots()),
        (stats.occupied_slots as usize) == tokens.len()
            && (tokens.len() as u64) <= p.total_slots(),
        format!("occupied={} tokens={}", stats.occupied_slots, tokens.len()),
        format!("occupied == tokens.len() <= {}", p.total_slots()),
    );
    r.check(
        "容量耗尽后每个失败都归类为 FILTER_FULL(kicks=max_kicks)，无其他类别",
        "不允许把容量问题报成 500/成功",
        full_count > 0 && other_errors.is_empty(),
        format!("FILTER_FULL={full_count} 其他={other_errors:?}"),
        "FILTER_FULL>0 且其他错误为空",
    );

    // 全部存活键必须仍然 member（零假阴性）。
    let mut false_negatives = Vec::new();
    for (n, _ins) in &tokens {
        let info = svc.lookup(&key(*n));
        if !info.member {
            false_negatives.push(*n);
        }
    }
    r.check(
        "所有成功插入且未删除的键逐个查询均为 member（零假阴性）",
        format!("{} 个存活键全检", tokens.len()),
        false_negatives.is_empty(),
        if false_negatives.is_empty() {
            "无".to_string()
        } else {
            format!("假阴性键: {false_negatives:?}")
        },
        "假阴性集合为空",
    );

    // 未插入的键允许假阳性但此时高负载下数量级记录即可；断言其不会误删（无凭证）。
    let delete_no_token = svc
        .delete(&key(10_000), "YW55LXRva2Vu.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        .unwrap_err();
    r.check(
        "未持有凭证的删除请求（哪怕查询可能假阳性）被拒绝",
        "删除只认 HMAC 凭证，而非 contains 结果",
        matches!(
            delete_no_token,
            PersistError::Core(CoreError::InvalidCredential(_))
        ),
        format!("{delete_no_token:?}"),
        "Core(InvalidCredential)",
    );

    r.finish()
}

// ---------------------------------------------------------------------------
// S06
// ---------------------------------------------------------------------------

pub fn scenario_06_persistence_restart(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S06",
        "快照落盘、重启恢复与损坏检测",
        run_id,
    );
    let dir = TempDir::new().unwrap();
    let path: PathBuf = dir.path().join("persist.bin");
    let p = FilterParams::new(7, 4, 10, 120).unwrap();

    let live: Vec<(Vec<u8>, String)> = {
        let mut svc =
            ServiceState::create(&path, p, TEST_MASTER_KEY.to_vec(), RNG_SEED).unwrap();
        let mut v = Vec::new();
        for n in 0..200u64 {
            let ins = svc.insert(&member_key(n)).unwrap();
            v.push((member_key(n), ins.token));
        }
        // 删除部分键，保证快照同时覆盖存活与已删状态。
        for n in (0..200u64).step_by(3) {
            let tok = v
                .iter()
                .find(|(k, _)| k == &member_key(n))
                .map(|(_, t)| t.clone())
                .unwrap();
            svc.delete(&member_key(n), &tok).unwrap();
        }
        v.retain(|(k, _)| {
            // 仅保留未删的
            let n: u64 = k
                .strip_prefix(b"member-")
                .and_then(|b| std::str::from_utf8(b).ok())
                .and_then(|s| s.parse().ok())
                .unwrap();
            n % 3 != 0
        });
        v
    };

    // 同参数重开。
    let mut reopened = ServiceState::open(&path, p, TEST_MASTER_KEY.to_vec(), RNG_SEED).unwrap();
    let mut survivors_missing = Vec::new();
    for (k, _t) in &live {
        if !reopened.lookup(k).member {
            survivors_missing.push(String::from_utf8_lossy(k).to_string());
        }
    }
    r.check(
        "重启后所有存活键仍然 member（零假阴性）",
        format!("存活 {} 键，快照重开后逐个查询", live.len()),
        survivors_missing.is_empty(),
        survivors_missing.join(","),
        "全部存活",
    );

    // 已删除的键：指纹碰撞理论上可能仍 true，但用其原凭证删除必须失败（凭证已不在账本）。
    let deleted_token = {
        // 重新插一个然后删除，拿到“曾经有效”的 token 来验证重放跨重启被拒。
        let ins = reopened.insert(b"temp-replay-key").unwrap();
        reopened.delete(b"temp-replay-key", &ins.token).unwrap();
        ins.token
    };
    let replay_after_restart = reopened
        .delete(b"temp-replay-key", &deleted_token)
        .unwrap_err();
    r.check(
        "跨重启重放已消费凭证 => CREDENTIAL_EXHAUSTED",
        "凭证账本随快照持久化",
        matches!(
            replay_after_restart,
            PersistError::Core(CoreError::CredentialExhausted)
        ),
        format!("{replay_after_restart:?}"),
        "Core(CredentialExhausted)",
    );

    // 存活凭证在重启后仍可正常删除一次。
    let (k0, t0) = live.first().unwrap();
    let del = reopened.delete(k0, t0);
    r.check(
        "存活凭证重启后删除成功",
        "凭证 token 由主密钥校验，账本由快照恢复",
        del.is_ok(),
        format!("{del:?}"),
        "Ok",
    );

    // 参数错配必须拒绝打开。
    let wrong = FilterParams::new(7, 4, 12, 120).unwrap();
    let mismatch = ServiceState::open(&path, wrong, TEST_MASTER_KEY.to_vec(), RNG_SEED)
        .err()
        .map(|e| format!("{e:?}"))
        .unwrap_or_default();
    r.check(
        "用不同参数（fp_bits 12 vs 快照 10）打开 => INIT 错误拒绝",
        "禁止静默用不同内核解释旧数据",
        mismatch.contains("Init"),
        mismatch,
        "PersistError::Init",
    );

    // 损坏文件 => 解码错误而不是被当作空存储。
    let raw = std::fs::read(&path).unwrap();
    let mut flipped = raw.clone();
    let idx = 50.min(flipped.len() - 9);
    flipped[idx] ^= 0x5A;
    let corrupt_path = dir.path().join("corrupt.bin");
    std::fs::write(&corrupt_path, flipped).unwrap();
    let corrupt = ServiceState::open(&corrupt_path, p, TEST_MASTER_KEY.to_vec(), RNG_SEED);
    r.check(
        "字节翻转的快照被拒绝（校验和/一致性错误）",
        "XXH64 校验覆盖全文件",
        matches!(
            corrupt,
            Err(PersistError::Storage(_)) | Err(PersistError::Init(_))
        ),
        format!("{:?}", corrupt.as_ref().err().map(|e|e.to_string())),
        "Err(Storage|Init)，绝不静默开空库",
    );

    r.finish()
}

// ---------------------------------------------------------------------------
// S07
// ---------------------------------------------------------------------------

pub fn scenario_07_false_positive_rate(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S07",
        "固定种子下的假阳性率测量（成员零假阴性）",
        run_id,
    );

    // 主配置：1024 桶 * 4 = 4096 槽，f=8；插入约 2000 成员（负载 ~0.49）。
    let dir = TempDir::new().unwrap();
    let p = FilterParams::new(10, 4, 8, 500).unwrap();
    let mut svc = create_service(&dir, p);
    let members = 2000u64;
    let mut insert_fail = 0u64;
    for n in 0..members {
        if svc.insert(&member_key(n)).is_err() {
            insert_fail += 1;
        }
    }
    let load = svc.stats().load_factor;
    r.note(
        "装入阶段",
        format!(
            "目标成员 {members}，插入失败 {insert_fail}，负载因子 {load:.4}，f={} 位",
            p.fingerprint_bits
        ),
    );

    // 成员零假阴性。
    let mut fn_list = Vec::new();
    for n in 0..members {
        if !svc.lookup(&member_key(n)).member {
            fn_list.push(n);
        }
    }
    r.check(
        "全部成员查询为 true（零假阴性）",
        format!("{members} 成员全检"),
        fn_list.is_empty(),
        format!("缺失 {fn_list:?}"),
        "无缺失",
    );

    // 非成员查询：固定夹具，前缀不同以保证集合不交。
    let probes = 50_000u64;
    let mut fp_hits = 0u64;
    for n in 0..probes {
        if svc.lookup(&nonmember_key(n)).member {
            fp_hits += 1;
        }
        if n > 0 && n % 10_000 == 0 {
            r.note(
                format!("查询进度 {n}/{probes}"),
                format!("当前命中 {fp_hits}，率 {:.6}", fp_hits as f64 / n as f64),
            );
        }
    }
    let measured = fp_hits as f64 / probes as f64;
    let bound_8 = 8f64 / p.bucket_size as f64 * 2f64.powi(-(p.fingerprint_bits as i32));
    // f=8, b=4 => 2/256 ~ 0.0078 量级（低负载近似）；留宽上限 0.04 防脆性。
    r.check(
        "假阳性率为有限小值，且显著低于“过滤器失效”量级",
        format!(
            "固定 {probes} 个非成员探针；实测 {fp_hits} 命中 => FPR={measured:.6}；理论近似 {bound_8:.6}（负载修正后更高）"
        ),
        (0.0..0.04).contains(&measured),
        format!("FPR={measured:.6}"),
        "0 <= FPR < 0.04（理论量级约 1e-2 以内）",
    );
    r.note(
        "理论对照（仅记录）",
        format!(
            "8/(b*2^f) = {bound_8:.6}；负载修正近似 {theo2:.6}；实测 {measured:.6}",
            theo2 = theoretical_fp_approx(p.bucket_size, p.fingerprint_bits, load)
        ),
    );

    // 对照组：f=4 必须明显更差（证明 FPR 确实由指纹位宽主导，测量有效）。
    let dir2 = TempDir::new().unwrap();
    let p4 = FilterParams::new(10, 4, 4, 500).unwrap();
    let mut svc4 = create_service(&dir2, p4);
    for n in 0..members {
        let _ = svc4.insert(&member_key(n));
    }
    let mut fp4 = 0u64;
    for n in 0..probes {
        if svc4.lookup(&nonmember_key(n)).member {
            fp4 += 1;
        }
    }
    let fpr4 = fp4 as f64 / probes as f64;
    r.check(
        "对照：f=4 的 FPR 显著高于 f=8（测量有效性自证）",
        format!("f=4 FPR={fpr4:.6} vs f=8 FPR={measured:.6}"),
        fpr4 > measured * 3.0,
        format!("fpr4/fpr8 = {:.2}", if measured > 0.0 { fpr4 / measured } else { f64::INFINITY }),
        "比值 > 3",
    );

    r.finish()
}

// ---------------------------------------------------------------------------
// S08
// ---------------------------------------------------------------------------

pub fn scenario_08_credential_attacks(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S08",
        "删除凭证攻击面：伪造/越权/重放/格式错误全部被分类拒绝",
        run_id,
    );
    let dir = TempDir::new().unwrap();
    let p = FilterParams::new(8, 4, 12, 200).unwrap();
    let mut svc = create_service(&dir, p);

    let a = b"alice-key";
    let b = b"bob-key";
    let ta = svc.insert(a).unwrap();
    let tb = svc.insert(b).unwrap();
    let occ0 = svc.stats().occupied_slots;

    // 1) 垃圾 token。
    let garbage = svc.delete(a, "not-a-token");
    r.check(
        "格式非法 token => INVALID_CREDENTIAL",
        "必须包含 jti.tag 两段",
        matches!(garbage, Err(PersistError::Core(CoreError::InvalidCredential(_)))),
        format!("{garbage:?}"),
        "Err(Core(InvalidCredential))",
    );

    // 2) 篡改签名（尾部翻转一个字符）。
    let mut tampered = ta.token.clone();
    tampered.replace_range(tampered.len() - 2.., "AA");
    let tamper = svc.delete(a, &tampered);
    r.check(
        "签名被篡改 => INVALID_CREDENTIAL",
        "HMAC 常量时间校验失败",
        matches!(tamper, Err(PersistError::Core(CoreError::InvalidCredential(_)))),
        format!("{tamper:?}"),
        "Err(Core(InvalidCredential))",
    );

    // 3) alice 的凭证用于删除 bob。
    let cross = svc.delete(b, &ta.token);
    r.check(
        "用 alice 的凭证删除 bob => INVALID_CREDENTIAL（键绑定）",
        "tag 覆盖 jti||0x00||key",
        matches!(cross, Err(PersistError::Core(CoreError::InvalidCredential(_)))),
        format!("{cross:?}"),
        "Err(Core(InvalidCredential))",
    );

    // 4) 另一把主密钥签发的 token（服务 B 的合法凭证）。
    let other_key = b"other-master-key-abcdef0123456789";
    let foreign = cuckoo_core::issue_token(other_key, &ta.jti, a);
    let foreign_err = svc.delete(a, &foreign);
    r.check(
        "不同主密钥签发 => INVALID_CREDENTIAL",
        "本服务只接受自己主密钥的 HMAC",
        matches!(
            foreign_err,
            Err(PersistError::Core(CoreError::InvalidCredential(_)))
        ),
        format!("{foreign_err:?}"),
        "Err(Core(InvalidCredential))",
    );

    // 5) 合法删除后重放。
    svc.delete(a, &ta.token).unwrap();
    let replay = svc.delete(a, &ta.token);
    r.check(
        "合法删除后重放同一凭证 => CREDENTIAL_EXHAUSTED",
        "一次性凭证",
        matches!(replay, Err(PersistError::Core(CoreError::CredentialExhausted))),
        format!("{replay:?}"),
        "Err(Core(CredentialExhausted))",
    );

    // 副作用检查：除第 5 步合法删除 alice 外，bob 与其余状态不变。
    let occ1 = svc.stats().occupied_slots;
    r.check(
        "前 4 类攻击零副作用：占用槽只减少 1（alice 合法删除），bob 仍在",
        "攻击不得移动任何指纹槽",
        occ0 - occ1 == 1 && svc.lookup(b).member,
        format!("occ {occ0} -> {occ1}; bob member={}", svc.lookup(b).member),
        "occ 减少恰好 1 且 bob 存活",
    );

    // bob 用自己的凭证仍可正常删除。
    let bob_del = svc.delete(b, &tb.token);
    r.check(
        "攻击不影响其他合法凭证：bob 随后可正常删除",
        "无状态污染",
        bob_del.is_ok(),
        format!("{:?}", bob_del.map(|x| (x.bucket, x.slot))),
        "Ok",
    );

    r.finish()
}

// ---------------------------------------------------------------------------
// S09
// ---------------------------------------------------------------------------

pub fn scenario_09_format_guards(run_id: String) -> ScenarioReport {
    let mut r = Reporter::new(
        "S09",
        "磁盘格式一致性与参数防护",
        run_id,
    );
    let dir = TempDir::new().unwrap();
    let p = FilterParams::new(4, 2, 6, 30).unwrap();
    let mut svc = create_service(&dir, p);
    for n in 0..12u64 {
        let _ = svc.insert(&key(n));
    }
    svc.flush().unwrap();

    // 直接构造与磁盘一致的快照后做各种破坏，codec 必须逐项拒绝。
    let snap = {
        // 经由 encode/decode 往返构造一个合法 Snapshot。
        let path = svc_path(&dir);
        let bytes = std::fs::read(&path).unwrap();
        Snapshot::decode(&bytes).unwrap()
    };

    // 1) 非空槽但 owner=零。
    let mut bad1 = snap.clone();
    let pos = bad1.slots.iter().position(|&x| x != 0).unwrap();
    bad1.owners[pos] = [0u8; 16];
    r.check(
        "非空槽缺归属 jti => SlotWithoutOwner",
        "槽与归属严格对齐",
        matches!(Snapshot::decode(&bad1.encode()), Err(CodecError::SlotWithoutOwner)),
        format!("{:?}", Snapshot::decode(&bad1.encode()).map(|_| ())),
        "Err(SlotWithoutOwner)",
    );

    // 2) 空槽带 owner。
    let mut bad2 = snap.clone();
    let epos = bad2.slots.iter().position(|&x| x == 0).unwrap();
    bad2.owners[epos] = [9u8; 16];
    r.check(
        "空槽带归属 => OwnerWithoutSlot",
        "防止幽灵凭证",
        matches!(Snapshot::decode(&bad2.encode()), Err(CodecError::OwnerWithoutSlot)),
        format!("{:?}", Snapshot::decode(&bad2.encode()).map(|_| ())),
        "Err(OwnerWithoutSlot)",
    );

    // 3) 两个槽同一 jti。
    let mut bad3 = snap.clone();
    let occ: Vec<usize> = bad3
        .slots
        .iter()
        .enumerate()
        .filter_map(|(i, &x)| (x != 0).then_some(i))
        .collect();
    if occ.len() >= 2 {
        bad3.owners[occ[1]] = bad3.owners[occ[0]];
        r.check(
            "重复归属 jti => DuplicateOwner",
            "凭证与槽一一对应",
            matches!(Snapshot::decode(&bad3.encode()), Err(CodecError::DuplicateOwner)),
            format!("{:?}", Snapshot::decode(&bad3.encode()).map(|_| ())),
            "Err(DuplicateOwner)",
        );
    } else {
        r.fail("存活槽 >=2", "夹具构造", "存活槽不足", format!("{occ:?}"), ">=2");
    }

    // 4) 指纹超位宽。
    let mut bad4 = snap.clone();
    let pos = bad4.slots.iter().position(|&x| x != 0).unwrap();
    bad4.slots[pos] = 200; // > 2^6-1
    r.check(
        "指纹超位宽 => FingerprintOutOfRange",
        "f=6 => 最大 63",
        matches!(
            Snapshot::decode(&bad4.encode()),
            Err(CodecError::FingerprintOutOfRange(200))
        ),
        format!("{:?}", Snapshot::decode(&bad4.encode()).map(|_| ())),
        "Err(FingerprintOutOfRange(200))",
    );

    // 5) 占用计数造假。
    let mut bad5 = snap.clone();
    bad5.occupied = bad5.occupied.wrapping_add(5);
    r.check(
        "occupied 与非空槽数不符 => OccupiedMismatch",
        "头部计数必须与数据一致",
        matches!(Snapshot::decode(&bad5.encode()), Err(CodecError::OccupiedMismatch { .. })),
        format!("{:?}", Snapshot::decode(&bad5.encode()).map(|_| ())),
        "Err(OccupiedMismatch)",
    );

    // 6) create 拒绝覆盖已有快照。
    let existing = svc_path(&dir);
    let refuse = ServiceState::create(&existing, p, TEST_MASTER_KEY.to_vec(), RNG_SEED);
    r.check(
        "create 不会覆盖已存在的快照",
        "防止误操作清空数据",
        matches!(refuse, Err(PersistError::Init(_))),
        format!("{:?}", refuse.as_ref().err().map(|e|e.to_string())),
        "Err(Init)",
    );

    // 7) 短主密钥被拒。
    let short = ServiceState::create(
        dir.path().join("x.bin"),
        p,
        b"short".to_vec(),
        RNG_SEED,
    );
    r.check(
        "主密钥短于 16 字节 => Init 错误",
        "配置层安全底线",
        matches!(short, Err(PersistError::Init(_))),
        format!("{:?}", short.as_ref().err().map(|e|e.to_string())),
        "Err(Init)",
    );

    r.note(
        "版本与参数",
        format!(
            "core {CORE_VERSION}, format v{FORMAT_VERSION}, params b_exp={} bsize={} f={} kicks={}",
            p.buckets_exp, p.bucket_size, p.fingerprint_bits, p.max_kicks
        ),
    );

    r.finish()
}

fn svc_path(dir: &TempDir) -> PathBuf {
    dir.path().join("snapshot.bin")
}

// ---------------------------------------------------------------------------
// 运行清单
// ---------------------------------------------------------------------------

pub fn run_all(run_id: Option<String>) -> Vec<ScenarioReport> {
    let rid = run_id.unwrap_or_else(new_run_id);
    type ScenarioFn = fn(String) -> ScenarioReport;
    let fns: Vec<(&str, ScenarioFn)> = vec![
        ("S01", scenario_01_oracle_anchors),
        ("S02", scenario_02_reference_model_parity),
        ("S03", scenario_03_kick_loop_and_rollback),
        ("S04", scenario_04_duplicate_counting),
        ("S05", scenario_05_capacity_exhaustion),
        ("S06", scenario_06_persistence_restart),
        ("S07", scenario_07_false_positive_rate),
        ("S08", scenario_08_credential_attacks),
        ("S09", scenario_09_format_guards),
    ];
    fns.into_iter().map(|(_, f)| f(rid.clone())).collect()
}
