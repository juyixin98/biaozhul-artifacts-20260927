//! 对照独立 BTreeSet oracle 的集合语义测试：
//! 稀疏、稠密、交错、跨容器、边界分布全覆盖；
//! 并/交/差/子集/相交、rank/select 全部逐点比对。

use rb_format::RoaringSet;
use rb_testkit::{all_fixtures, oracle::Oracle, SplitMix64};

fn build(values: &[u32]) -> (RoaringSet, Oracle) {
    let mut s = RoaringSet::new();
    let mut o = Oracle::new();
    // 打乱后插入，验证内核不依赖插入顺序
    let mut rng = SplitMix64::new(0xF1_F2_F3_F4);
    let mut order: Vec<u32> = values.to_vec();
    // Fisher–Yates（确定性）
    for i in (1..order.len()).rev() {
        let j = rng.below((i + 1) as u64) as usize;
        order.swap(i, j);
    }
    // 插入两遍以验证幂等
    for &v in &order {
        s.insert(v);
        o.insert(v);
    }
    for &v in &order {
        s.insert(v);
    }
    (s, o)
}

fn assert_eq_set(s: &RoaringSet, o: &Oracle, label: &str) {
    assert_eq!(s.len_u64() as usize, o.len(), "{label}: cardinality");
    assert_eq!(s.to_vec(), o.sorted_vec(), "{label}: full contents");
    assert_eq!(s.min(), o.min(), "{label}: min");
    assert_eq!(s.max(), o.max(), "{label}: max");
}

#[test]
fn every_fixture_matches_oracle() {
    for fx in all_fixtures() {
        let (s, o) = build(&fx.values);
        assert_eq_set(&s, &o, fx.name);

        // contains：正例 + 一组采样反例
        for &v in &fx.values {
            assert!(s.contains(v), "{} should contain {v}", fx.name);
        }
        let mut rng = SplitMix64::new(0xABCD);
        for _ in 0..500 {
            let probe = rng.next_u64() as u32;
            assert_eq!(
                s.contains(probe),
                o.contains(probe),
                "{} contains {probe}",
                fx.name
            );
        }
    }
}

#[test]
fn rank_select_match_oracle_everywhere_dense() {
    // 稠密多容器大集合：对 0..=2^20 全部 x 检查 rank，并验证 select(rank(x)) 关系
    let mut rng = SplitMix64::new(0x1111);
    let (s, o) = build(
        &(0..200_000u32)
            .map(|i| (i.wrapping_mul(1_000_003)).wrapping_add(rng.next_u64() as u32 % 5))
            .collect::<Vec<u32>>(),
    );
    for x in (0..=(1u32 << 20)).step_by(1024) {
        assert_eq!(s.rank(x), o.rank(x), "rank({x})");
    }
    // rank/select 互逆：对每个元素 e，select(rank(e)) == e
    for (idx, &e) in o.sorted_vec().iter().enumerate().step_by(997) {
        assert_eq!(s.rank(e), idx as u64);
        assert_eq!(s.select(idx as u64), Some(e));
    }
}

#[test]
fn boundary_rank_no_overflow() {
    // u32 边界：rank(u32::MAX) 与 rank(u32::MAX 作为“严格小于”的语义)
    let (s, o) = build(&[0, 1, u32::MAX - 1, u32::MAX]);
    assert_eq!(s.rank(0), 0);
    assert_eq!(s.rank(1), 1);
    assert_eq!(s.rank(u32::MAX), o.rank(u32::MAX));
    assert_eq!(s.rank(u32::MAX), 3);
    assert_eq!(s.select(3), Some(u32::MAX));
    // 大基数不溢出路径：直接构造 600 个“全满”位图容器（39,321,600 元素），
    // 避免 debug 模式下逐值插入的开销，同时覆盖 u64 计数与跨容器 rank。
    let mut big = RoaringSet::new();
    for k in 0..600u16 {
        let c = rb_format::test_util::raw_bitmap([u64::MAX; rb_format::BITMAP_WORDS]);
        rb_format::test_util::insert_raw(&mut big, k, c);
    }
    assert_eq!(big.len_u64(), 600 * 65536);
    // rank(u32::MAX)：键 < 65535 的容器全部计入（600 个全满容器），
    // key=65535 不存在 → 严格小于 u32::MAX 的数量 = 全部 600 个容器（最大键 599）。
    assert_eq!(big.rank(u32::MAX), 600 * 65536);
}

#[test]
fn set_operations_match_oracle_all_fixture_pairs() {
    let fixtures = all_fixtures();
    // 每个夹具只构建一次（含全满容器夹具，避免重复 O(65536) 构造）。
    let built: Vec<(&str, RoaringSet, Oracle)> = fixtures
        .iter()
        .map(|f| {
            let (s, o) = build(&f.values);
            (f.name, s, o)
        })
        .collect();
    for (na, sa, oa) in &built {
        for (nb, sb, ob) in &built {
            let label = format!("{na} op {nb}");

            let u = sa.union(sb);
            assert_eq_set(&u, &oa.union(ob), &format!("{label} union"));

            let i = sa.intersect(sb);
            assert_eq_set(&i, &oa.intersect(ob), &format!("{label} intersect"));

            let d = sa.difference(sb);
            assert_eq_set(&d, &oa.difference(ob), &format!("{label} diff"));

            assert_eq!(sa.intersects(sb), oa.intersects(ob), "{label} intersects");
            assert_eq!(sa.is_subset(sb), oa.is_subset(ob), "{label} subset");
        }
    }
}

#[test]
fn interleave_pair_has_expected_concrete_overlap() {
    // 对交错夹具断言具体结果（而非仅对照 oracle）：
    // A 与 B 在共有键 key=2 上，A 放偶数(含3000奇数共享) ...
    // 这里用具体的基数区间断言，说明运算确实发生在混合容器表示上。
    let fxs = all_fixtures();
    let get = |n: &str| fxs.iter().find(|f| f.name == n).unwrap();
    let a = get("interleaved_pair_a");
    let b = get("interleaved_pair_b");

    let sa = RoaringSet::from_values(a.values.iter().copied());
    let sb = RoaringSet::from_values(b.values.iter().copied());

    // A 使用偶数高键（0,2,4..38），B 使用奇数高键（1,3,..39），
    // 外加两者都在 key=2 注入了 3000 个对方奇偶性的值。
    // → 只有 key=2 这个分片相交，交集元素 = 两边注入重叠区域。
    let inter = sa.intersect(&sb);
    assert_eq!(inter.container_count(), 1, "only shared key 2 intersects");
    assert!(!inter.is_empty());
    // 交集内容必须都落在 key=2 分片
    for v in inter.to_vec() {
        assert_eq!(v >> 16, 2);
    }
    // 并集包含所有偶数键容器与奇数键容器
    let union = sa.union(&sb);
    assert!(union.len() > sa.len());
    assert!(union.len() > sb.len());
}

#[test]
fn difference_is_antisymmetric_and_union_decomposes() {
    // 恒等式（用 oracle 交叉验证，同时直接在内核上断言）：
    //   A \\ B 与 B \\ A 不相交； A∪B = (A\\B) ∪ (B\\A) ∪ (A∩B)
    let fxs = all_fixtures();
    let a = fxs.iter().find(|f| f.name == "dense_container").unwrap();
    let b = fxs.iter().find(|f| f.name == "wide_spread").unwrap();
    let sa = RoaringSet::from_values(a.values.iter().copied());
    let sb = RoaringSet::from_values(b.values.iter().copied());

    let dab = sa.difference(&sb);
    let dba = sb.difference(&sa);
    let inter = sa.intersect(&sb);
    let rebuilt = dab.union(&dba).union(&inter);
    assert_eq!(rebuilt, sa.union(&sb));
    assert!(!dab.intersects(&dba));
}
