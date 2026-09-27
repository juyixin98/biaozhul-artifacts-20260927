//! 集成测试：FM 内核正确性。
//!
//! 覆盖需求点：
//! - 高重复文本 + 重叠匹配位置（`aaaa…`、周期串）；
//! - 二进制零字节（0x00 与哨兵 0 的编码不冲突）；
//! - 多模式批量对照独立朴素扫描（参照来自 [`fm_index_service::naive`]，非内核自证）；
//! - 手工硬编码夹具 banana 的具体数值（不依赖运行时计算生成期望）；
//! - 空模式 / 超长模式的显式语义；
//! - 多种采样步长与 rank 块大小组合；
//! - 较大文本（数万字节）端到端一致。
//!
//! 每个用例通过 `TestLog` 落盘运行编号、关键中间状态（区间、LF 计数等）与判断理由。

mod common;

use common::{Harness, TestLog, preview, pseudo_bytes};
use fm_index_service::fm::FmIndex;
use fm_index_service::naive::{naive_count, naive_locate};

/// 手工硬编码的 banana 期望（人工推导，非被测实现产出）。
///
/// text=b"banana"（m=6, n=7），SA=[6,5,3,1,0,4,2]，BWT=[a,n,n,b,$,a,a]，
/// sentinel_row=4（sa=0 行），C：C[$]=0, C[a]=1, C[b]=4, C[n]=5。
const BANANA_EXPECTED: &[(&[u8], u64, &[u64])] = &[
    (b"ana", 2, &[1, 3]),
    (b"na", 2, &[2, 4]),
    (b"a", 3, &[1, 3, 5]),
    (b"banana", 1, &[0]),
    (b"b", 1, &[0]),
    (b"xyz", 0, &[]),
    (b"anan", 1, &[1]),
    (b"", 7, &[0, 1, 2, 3, 4, 5, 6]), // 空模式：count=n=7，locate=0..=6
];

#[test]
fn banana_hardcoded_answers() {
    let mut log = TestLog::new("banana_hardcoded_answers");
    let idx = FmIndex::build(b"banana", 64, 2).expect("建 banana 索引");
    log.record("sentinel_row", idx.sentinel_row());
    log.assert_eq_log(
        &idx.sentinel_row(),
        &4u64,
        "banana sentinel_row=4（sa=0 行）",
    );
    log.assert_eq_log(&idx.encoded_len(), &7u64, "n=7");
    log.assert_eq_log(&idx.text_len(), &6u64, "m=6");

    for (pat, exp_count, exp_locs) in BANANA_EXPECTED {
        let iv = idx.search(pat);
        log.record(
            format!("pattern {} interval", preview(pat, 16)),
            (iv.lo, iv.hi, iv.count()),
        );
        log.assert_eq_log(&iv.count(), exp_count, "count 半开区间宽度");
        log.assert_eq_log(&iv.is_empty(), &(*exp_count == 0), "空区间当且仅当无命中");
        let locs = idx.locate(pat).expect("locate");
        log.assert_eq_log(&locs, &exp_locs.to_vec(), "locate 具体位置（含重叠）");
    }
    log.note("所有期望值来自人工推导的固定夹具，不来自内核自身");
}

#[test]
fn empty_and_overlong_pattern_semantics() {
    let mut log = TestLog::new("empty_and_overlong_pattern_semantics");
    let idx = FmIndex::build(b"abc", 2, 1).unwrap();

    // 空模式：全集区间 [0,n)，count=n=m+1，locate=0..=m
    let iv = idx.search(b"");
    log.record("empty interval", (iv.lo, iv.hi));
    assert!(iv.lo == 0 && iv.hi == 4, "空模式必须是 [0,4) 全集");
    assert_eq!(idx.count(b""), 4);
    assert_eq!(idx.locate(b"").unwrap(), vec![0, 1, 2, 3]);

    // 超长模式：显式空区间（lo==hi==0），不跨哨兵匹配
    for p in [b"abcd".as_slice(), b"abcabc", &[0u8, 0, 0, 0, 0]] {
        let iv = idx.search(p);
        log.record(
            format!("overlong {} interval", preview(p, 16)),
            (iv.lo, iv.hi),
        );
        assert!(iv.is_empty(), "超长模式必须返回 lo==hi 的空区间");
        assert!(idx.locate(p).unwrap().is_empty());
    }
    // 边界：长度 == m 仍合法
    log.assert_eq_log(&idx.count(b"abc"), &1u64, "等长模式正常匹配");
}

#[test]
fn high_repetition_overlapping_matches() {
    let mut log = TestLog::new("high_repetition_overlapping_matches");
    let text = b"aaaaaaaaaaaaaaaaaaaa"; // 20 个 a
    let idx = FmIndex::build(text, 4, 3).unwrap();
    for plen in 1usize..=20 {
        let pat = vec![b'a'; plen];
        let fc = idx.count(&pat);
        let nc = naive_count(text, &pat);
        log.assert_eq_log(&fc, &nc, format!("aaaa×20 count(pat_len={plen})"));
        assert_eq!(fc, (20 - plen + 1) as u64, "纯重复串命中数= m-p+1");
        let fl = idx.locate(&pat).unwrap();
        assert_eq!(
            fl,
            (0..=(20 - plen) as u64).collect::<Vec<_>>(),
            "全部重叠位置连续"
        );
    }
    // 周期串 "ab"×20（精确 40 字节）：模式 "abab" 有 19 个重叠起点
    let text2 = b"ab".repeat(20);
    assert_eq!(text2.len(), 40);
    let idx2 = FmIndex::build(&text2, 5, 2).unwrap();
    assert_eq!(idx2.count(b"abab"), naive_count(&text2, b"abab"));
    assert_eq!(idx2.locate(b"abab").unwrap(), naive_locate(&text2, b"abab"));
    log.assert_eq_log(
        &idx2.locate(b"abab").unwrap().len(),
        &19usize,
        "ab×20 中 abab 的重叠命中=19",
    );
}

#[test]
fn binary_zero_bytes_no_sentinel_collision() {
    let mut log = TestLog::new("binary_zero_bytes_no_sentinel_collision");
    // 含大量 0x00 与 0xff：正文 0x00 编码为 1，哨兵为 0，必须互不干扰。
    let mut text = vec![0u8; 64];
    text[10] = 0xff;
    text[40] = 0xff;
    text[63] = 0x01;
    let idx = FmIndex::build(&text, 7, 4).unwrap();

    let patterns: [&[u8]; 7] = [
        &[0x00],
        &[0x00, 0x00],
        &[0x00, 0x00, 0x00],
        &[0xff],
        &[0xff, 0x00],
        &[0x00, 0xff],
        &[0x01],
    ];
    for p in patterns {
        let fc = idx.count(p);
        let nc = naive_count(&text, p);
        let fl = idx.locate(p).unwrap();
        let nl = naive_locate(&text, p);
        log.record(
            format!(
                "zero-text pattern={} fm_count={fc} naive_count={nc}",
                preview(p, 8)
            ),
            &fl,
        );
        log.assert_eq_log(&fc, &nc, "零字节文本 count");
        log.assert_eq_log(&fl, &nl, "零字节文本 locate（重叠）");
    }
    // 全零文本是最容易和哨兵混淆的极端情形
    let z = vec![0u8; 100];
    let idxz = FmIndex::build(&z, 16, 5).unwrap();
    assert_eq!(idxz.count(&[0; 1]), 100);
    assert_eq!(
        idxz.locate(&[0; 1]).unwrap(),
        (0..100u64).collect::<Vec<_>>()
    );
    assert_eq!(idxz.count(&[0; 50]), 51);
    log.note("100 个 0x00 文本中单零字节命中 100 次，证明哨兵编码未污染正文域");
}

#[test]
fn sample_and_block_parameter_sweep() {
    let mut log = TestLog::new("sample_and_block_parameter_sweep");
    // 高重复 + 混合内容，放大采样定位的 LF 行走路径差异。
    let text = pseudo_bytes(7, 500, 4);
    let patterns: Vec<Vec<u8>> = vec![
        vec![0],
        vec![1],
        vec![0, 0],
        vec![1, 2],
        vec![3, 3, 3],
        vec![0, 1, 2, 3],
        vec![2, 2, 2, 2, 2],
        vec![],
        pseudo_bytes(99, 8, 4), // 随机长模式（大概率无命中，也测空区间路径）
    ];
    let mut case = 0u32;
    for rank_block in [1u32, 2, 3, 16, 257] {
        for sample_step in 1u32..=9 {
            let idx = FmIndex::build(&text, rank_block, sample_step).expect("参数组合可建索引");
            for pat in &patterns {
                case += 1;
                let fc = idx.count(pat);
                let nc = naive_count(&text, pat);
                let fl = idx.locate(pat).unwrap();
                let nl = naive_locate(&text, pat);
                if fc != nc || fl != nl {
                    log.record(
                        format!(
                            "MISMATCH case#{case} block={rank_block} step={sample_step} pat={}",
                            preview(pat, 12)
                        ),
                        (&fc, &nc, &fl, &nl),
                    );
                    panic!("参数组合不一致，见日志 case#{case}");
                }
            }
        }
    }
    log.record("通过组合数", case);
    log.note("5 种 rank_block × 9 种 sample_step × 9 模式全部与朴素扫描一致");
}

#[test]
fn large_text_end_to_end_matches_naive() {
    let mut log = TestLog::new("large_text_end_to_end_matches_naive");
    // 20_000 字节小字母表文本：覆盖更长的 LF 行走与检查点跨块。
    let text = pseudo_bytes(20260927, 20_000, 6);
    let h = Harness::new();
    h.build_index("big", &text, 256, 16);
    let svc = &h.service;
    let probes: [&[u8]; 6] = [
        b"\x00\x00",
        b"\x01\x02\x03",
        b"\x05\x05\x05\x05",
        b"\x02\x04",
        &pseudo_bytes(123, 3, 6),
        b"",
    ];
    for p in probes {
        let r = svc.search("big", p, false).expect("查询");
        let nl = naive_locate(&text, p);
        let nc = naive_count(&text, p);
        log.record(
            format!("large pat={} count={} naive={}", preview(p, 8), r.count, nc),
            r.locations.len(),
        );
        assert_eq!(r.count, nc);
        assert_eq!(r.locations, nl);
    }
}

#[test]
fn invalid_build_inputs_are_classified() {
    // 非法输入必须是 InvalidInput 类别，而不是笼统失败。
    assert!(matches!(
        FmIndex::build(b"", 16, 4),
        Err(fm_index_service::FmError::InvalidInput { .. })
    ));
    assert!(matches!(
        FmIndex::build(b"x", 0, 4),
        Err(fm_index_service::FmError::InvalidInput { .. })
    ));
    assert!(matches!(
        FmIndex::build(b"x", 16, 0),
        Err(fm_index_service::FmError::InvalidInput { .. })
    ));
    let mut log = TestLog::new("invalid_build_inputs_are_classified");
    log.note("空文本 / rank_block=0 / sample_step=0 均返回 invalid_input");
}

#[test]
fn trace_exposes_intermediate_intervals() {
    // trace 记录每一步处理前后区间，首步来自全集，空区间后立即终止。
    let mut log = TestLog::new("trace_exposes_intermediate_intervals");
    let idx = FmIndex::build(b"banana", 64, 2).unwrap();
    let (iv, steps) = idx.search_traced(b"ana", true);
    log.record("ana 最终区间", (iv.lo, iv.hi));
    assert_eq!(iv.count(), 2);
    assert_eq!(steps.len(), 3, "3 个符号应有 3 步");
    assert_eq!(
        (steps[0].lo_before, steps[0].hi_before),
        (0, 7),
        "首步从全集开始"
    );
    assert!(steps.iter().all(|s| !s.emptied), "ana 全程不应提前清空");

    let (iv2, steps2) = idx.search_traced(b"xyz", true);
    assert!(iv2.is_empty());
    assert_eq!(steps2.len(), 1, "无命中应在首个符号后终止并标记 emptied");
    assert!(steps2[0].emptied);
    log.record("xyz trace", steps2.len());
}
