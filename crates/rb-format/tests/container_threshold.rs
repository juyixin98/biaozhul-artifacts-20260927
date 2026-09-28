//! 容器阈值与表示切换的具体断言（不只是“能调用”）。

use rb_format::test_util::{bitmap_with_bits, container_from_lows, is_array};
use rb_format::Container;
use rb_format::ARRAY_MAX_CARDINALITY;
use rb_testkit::rng::SplitMix64;

#[test]
fn empty_is_array() {
    let c = container_from_lows(vec![]);
    assert!(is_array(&c));
    assert_eq!(c.len(), 0);
    assert!(c.is_empty());
}

#[test]
fn exactly_threshold_stays_array() {
    // 4096 个元素：必须仍是稀疏数组（规范分界点）。
    let lows: Vec<u16> = (0..ARRAY_MAX_CARDINALITY as u16).collect();
    let c = container_from_lows(lows);
    assert!(is_array(&c), "cardinality 4096 must canonicalize to Array");
    assert_eq!(c.len(), ARRAY_MAX_CARDINALITY);
}

#[test]
fn one_above_threshold_becomes_bitmap() {
    let lows: Vec<u16> = (0..=(ARRAY_MAX_CARDINALITY as u16)).collect(); // 4097
    let c = container_from_lows(lows);
    assert!(
        !is_array(&c),
        "cardinality 4097 must canonicalize to Bitmap"
    );
    assert_eq!(c.len(), ARRAY_MAX_CARDINALITY + 1);
}

#[test]
fn bitmap_shrinks_back_to_array_below_threshold() {
    // 用 5000 个值得到位图，再逐个删到 4096 以下，必须转回数组。
    let mut rng = SplitMix64::new(0xBEEF);
    let mut chosen = std::collections::BTreeSet::new();
    while chosen.len() < 5000 {
        chosen.insert(rng.next_u64() as u16);
    }
    let mut c = bitmap_with_bits(chosen.iter().copied());
    assert!(!is_array(&c));

    // 删到只剩 4096。
    let to_remove: Vec<u16> = chosen.iter().skip(4096).copied().collect();
    for v in to_remove {
        assert!(c.remove(v));
    }
    assert_eq!(c.len(), ARRAY_MAX_CARDINALITY);
    assert!(is_array(&c), "shrinking to 4096 must return to Array");
}

#[test]
fn insert_remove_and_contains_roundtrip() {
    let mut c = Container::new();
    assert!(c.insert(12345));
    assert!(c.insert(1));
    assert!(!c.insert(12345), "duplicate insert must report false");
    assert!(c.contains(12345));
    assert_eq!(c.len(), 2);
    // 有序遍历
    assert_eq!(c.to_vec(), vec![1, 12345]);
    assert!(c.remove(1));
    assert!(!c.contains(1));
    assert!(!c.remove(1), "removing absent must report false");
}

#[test]
fn unsorted_duplicate_input_is_normalized() {
    // from_values 必须排序去重
    let c = container_from_lows(vec![5, 5, 1, 4096, 1, 2]);
    assert_eq!(c.to_vec(), vec![1, 2, 5, 4096]);
    assert_eq!(c.len(), 4);
}

#[test]
fn container_rank_select_specific_values() {
    let c = container_from_lows(vec![10, 20, 30, 65535]);
    // rank(x) = 严格小于 x 的个数
    assert_eq!(c.rank_full(0), 0);
    assert_eq!(c.rank_full(10), 0);
    assert_eq!(c.rank_full(11), 1);
    assert_eq!(c.rank_full(20), 1);
    assert_eq!(c.rank_full(21), 2);
    assert_eq!(c.rank_full(65535), 3);
    assert_eq!(c.rank_full(65536), 4);
    // select
    assert_eq!(c.select(0), Some(10));
    assert_eq!(c.select(2), Some(30));
    assert_eq!(c.select(3), Some(65535));
    assert_eq!(c.select(4), None);
}

#[test]
fn bitmap_rank_select_matches_words() {
    // 稠密容器上的 rank/select 具体值
    let lows: Vec<u16> = (0..5000u16).map(|i| i * 7).filter(|&v| v < 60000).collect();
    let n = lows.len();
    let c = bitmap_with_bits(lows.clone());
    assert!(!is_array(&c));
    // 与线性参考逐一核对 rank/select
    for (i, &v) in lows.iter().enumerate() {
        assert_eq!(c.select(i), Some(v), "select({i})");
    }
    for &v in &[0u32, 1, 69, 70, 59999, 60000, 65535, 65536] {
        let expected = lows.iter().filter(|&&x| (x as u32) < v).count();
        assert_eq!(c.rank_full(v), expected, "rank({v})");
    }
    assert_eq!(c.select(n), None);
}
