//! 二进制格式 v1 的往返测试：所有夹具 + 阈值边界，解码结果逐值等于 oracle，
//! 且物理容器类型在编码前后保持（Array 不会被错误存成 Bitmap）。

use rb_format::codec;
use rb_format::test_util::is_array;
use rb_format::RoaringSet;
use rb_testkit::{all_fixtures, oracle::Oracle};

#[test]
fn all_fixtures_roundtrip_byte_for_byte() {
    for fx in all_fixtures() {
        let s = RoaringSet::from_values(fx.values.iter().copied());
        let o = Oracle::from_values(fx.values.iter().copied());

        let bytes = codec::encode(&s);
        // 魔数与版本
        assert_eq!(&bytes[0..4], b"RBS1", "{} magic", fx.name);
        assert_eq!(
            u32::from_le_bytes(bytes[4..8].try_into().unwrap()),
            0x0001_0000
        );

        let decoded = codec::decode(&bytes).expect(fx.name);
        assert_eq!(decoded.len_u64() as usize, o.len(), "{} len", fx.name);
        assert_eq!(decoded.to_vec(), o.sorted_vec(), "{} values", fx.name);
        assert_eq!(decoded, s, "{} decode must equal pre-encode set", fx.name);

        // 编码确定性：同一集合两次编码字节完全一致
        assert_eq!(
            codec::encode(&decoded),
            bytes,
            "{} deterministic bytes",
            fx.name
        );
    }
}

#[test]
fn threshold_container_type_survives_roundtrip() {
    // 4096 → Array；4097 → Bitmap。解码后逐容器验证物理类型。
    let values_4096: Vec<u32> = (0..4096).collect();
    let values_4097: Vec<u32> = (0..4097).collect();

    let a = RoaringSet::from_values(values_4096.iter().copied());
    let b = RoaringSet::from_values(values_4097.iter().copied());

    let da = codec::decode(&codec::encode(&a)).unwrap();
    let db = codec::decode(&codec::encode(&b)).unwrap();

    assert!(is_array(da.container(0).unwrap()), "4096 stored as array");
    assert!(!is_array(db.container(0).unwrap()), "4097 stored as bitmap");

    // 编码后的负载长度：array = 4096*2 = 8192；bitmap 恒为 8192。
    // 两者体积相同，但类型标签不同——检查标签字节。
    let ba = codec::encode(&a);
    let bb = codec::encode(&b);
    // 目录项位于偏移 16，标签在 key(2 字节) 之后
    assert_eq!(ba[16 + 2], 1); // TAG_ARRAY
    assert_eq!(bb[16 + 2], 2); // TAG_BITMAP
}

#[test]
fn multi_container_offsets_are_contiguous() {
    // 跨多个容器（混合 array/bitmap）时，目录偏移必须连续无重叠。
    let fxs = all_fixtures();
    let mut combined = std::collections::BTreeSet::new();
    for name in [
        "sparse_tiny",
        "dense_container",
        "high_dense",
        "boundary_values",
    ] {
        let fx = fxs.iter().find(|f| f.name == name).unwrap();
        combined.extend(fx.values.iter().copied());
    }
    let s = RoaringSet::from_values(combined.iter().copied());
    let bytes = codec::encode(&s);

    let count = u32::from_le_bytes(bytes[8..12].try_into().unwrap()) as usize;
    assert_eq!(count, s.container_count());

    // 手工解析目录，验证 offset 单调连续
    let mut prev_end = (count * 16) as u32;
    for i in 0..count {
        let base = 16 + i * 16;
        let plen = u32::from_le_bytes(bytes[base + 4..base + 8].try_into().unwrap());
        let off = u32::from_le_bytes(bytes[base + 8..base + 12].try_into().unwrap());
        assert_eq!(off, prev_end, "entry {i} offset");
        prev_end += plen;
    }

    // 往返内容一致
    let decoded = codec::decode(&bytes).unwrap();
    assert_eq!(decoded.to_vec(), combined.into_iter().collect::<Vec<u32>>());
}

#[test]
fn empty_set_roundtrip() {
    let s = RoaringSet::new();
    let bytes = codec::encode(&s);
    // 头16 + body crc 4 = 20 字节
    assert_eq!(bytes.len(), 20);
    let decoded = codec::decode(&bytes).unwrap();
    assert!(decoded.is_empty());
}
