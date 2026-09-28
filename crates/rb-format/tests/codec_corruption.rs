//! 文件损坏拒绝：逐类篡改合法字节流，断言解码返回**具体的失败类别**，
//! 而不是笼统的 Err。覆盖魔数/版本/CRC/标签/键序/偏移/长度/有序性/基数/阈值。

use rb_format::codec;
use rb_format::{CodecError, RoaringSet, ARRAY_MAX_CARDINALITY, BITMAP_WORDS};

/// 构建一个含 2 个容器（一个 array、一个 bitmap）的合法样本。
fn sample_bytes() -> Vec<u8> {
    let mut s = RoaringSet::new();
    // key=1，array，3 个元素
    for v in [1u32, 2, 100] {
        s.insert((1u32 << 16) | v);
    }
    // key=400，bitmap，5000 个元素
    for i in 0..5000u32 {
        s.insert((400u32 << 16) | (i * 11 % 65536));
    }
    codec::encode(&s)
}

fn expect_category(bytes: &[u8], pred: impl Fn(&CodecError) -> bool, desc: &str) {
    match codec::decode(bytes) {
        Ok(s) => panic!("corruption {desc} was ACCEPTED, set len={}", s.len_u64()),
        Err(e) => assert!(pred(&e), "corruption {desc} gave wrong category: {e:?}"),
    }
}

#[test]
fn rejects_truncation() {
    let good = sample_bytes();
    for cut in [0usize, 4, 15, 16, 19, good.len() - 1] {
        expect_category(
            &good[..cut],
            |e| matches!(e, CodecError::Truncated { .. }),
            &format!("truncate@{cut}"),
        );
    }
}

#[test]
fn rejects_bad_magic() {
    let mut b = sample_bytes();
    b[0] = b'X';
    expect_category(&b, |e| matches!(e, CodecError::BadMagic(_)), "magic");
}

#[test]
fn rejects_bad_version() {
    let mut b = sample_bytes();
    b[4..8].copy_from_slice(&99u32.to_le_bytes());
    // 版本变化后头部 CRC 也会失败；版本检查在 CRC 之前，应得 UnsupportedVersion。
    expect_category(
        &b,
        |e| matches!(e, CodecError::UnsupportedVersion(99)),
        "version",
    );
}

#[test]
fn rejects_header_crc_tamper() {
    let mut b = sample_bytes();
    b[8] = b[8].wrapping_add(1); // 篡改 container_count
    expect_category(
        &b,
        |e| matches!(e, CodecError::HeaderChecksumMismatch { .. }),
        "header crc",
    );
}

#[test]
fn rejects_body_crc_tamper() {
    let mut b = sample_bytes();
    // 翻转体区中一个负载字节（定位在第一个 array 负载区）
    let n_containers = u32::from_le_bytes(b[8..12].try_into().unwrap()) as usize;
    let body_payload_start = 16 + n_containers * 16;
    b[body_payload_start] ^= 0xFF;
    expect_category(
        &b,
        |e| matches!(e, CodecError::BodyChecksumMismatch { .. }),
        "body crc",
    );
}

// ---- 需要手工构造目录的损坏（保持 CRC 自洽） ----

/// 单个目录项的字段覆盖：(entry_index, tag, payload_len, offset, cardinality, reserved)。
type Override<'a> = &'a [(
    usize,
    Option<u8>,
    Option<u32>,
    Option<u32>,
    Option<u32>,
    Option<u8>,
)];

struct Builder {
    entries: Vec<RawEntry>,
}

struct RawEntry {
    key: u16,
    tag: u8,
    payload: Vec<u8>,
    cardinality: u32,
}

impl Builder {
    fn new() -> Self {
        Builder {
            entries: Vec::new(),
        }
    }

    fn add(&mut self, key: u16, tag: u8, payload: Vec<u8>, cardinality: u32) {
        self.entries.push(RawEntry {
            key,
            tag,
            payload,
            cardinality,
        });
    }

    /// 允许故意制造错误的字段。
    fn build_with(
        &self,
        overrides: Override<'_>,
        // 对最终字节的额外篡改（在 CRC 计算之后）
        post_tamper: impl FnOnce(&mut Vec<u8>),
    ) -> Vec<u8> {
        let count = self.entries.len();
        let dir_len = count * 16;
        let mut out = Vec::new();
        out.extend_from_slice(b"RBS1");
        out.extend_from_slice(&0x0001_0000u32.to_le_bytes());
        out.extend_from_slice(&(count as u32).to_le_bytes());
        let hcrc = rb_format::crc32c::checksum(&out[..12]);
        out.extend_from_slice(&hcrc.to_le_bytes());

        // 先写目录
        let mut cursor = dir_len as u32;
        let mut meta = Vec::new();
        for (i, e) in self.entries.iter().enumerate() {
            let ov = overrides.iter().find(|(idx, ..)| *idx == i);
            let tag = ov.and_then(|o| o.1).unwrap_or(e.tag);
            let plen = ov.and_then(|o| o.2).unwrap_or(e.payload.len() as u32);
            let off = ov.and_then(|o| o.3).unwrap_or(cursor);
            let card = ov.and_then(|o| o.4).unwrap_or(e.cardinality);
            let reserved = ov.and_then(|o| o.5).unwrap_or(0);

            out.extend_from_slice(&e.key.to_le_bytes());
            out.push(tag);
            out.push(reserved);
            out.extend_from_slice(&plen.to_le_bytes());
            out.extend_from_slice(&off.to_le_bytes());
            out.extend_from_slice(&card.to_le_bytes());
            meta.push((plen, cursor));
            cursor += e.payload.len() as u32;
        }
        // 再写真实负载
        for e in &self.entries {
            out.extend_from_slice(&e.payload);
        }
        let bcrc = rb_format::crc32c::checksum(&out[16..]);
        out.extend_from_slice(&bcrc.to_le_bytes());
        let _ = meta;
        post_tamper(&mut out);
        out
    }
}

fn array_payload(lows: &[u16]) -> Vec<u8> {
    let mut v = Vec::new();
    for l in lows {
        v.extend_from_slice(&l.to_le_bytes());
    }
    v
}

fn bitmap_payload(set_lows: impl Fn(usize) -> bool) -> Vec<u8> {
    let mut words = vec![0u64; BITMAP_WORDS];
    for low in 0..65536usize {
        if set_lows(low) {
            words[low / 64] |= 1u64 << (low % 64);
        }
    }
    let mut v = Vec::with_capacity(BITMAP_WORDS * 8);
    for w in words {
        v.extend_from_slice(&w.to_le_bytes());
    }
    v
}

#[test]
fn rejects_unknown_container_tag() {
    let mut b = Builder::new();
    b.add(1, 1, array_payload(&[1, 2, 3]), 3);
    let bytes = b.build_with(&[(0, Some(9), None, None, None, None)], |_| {});
    expect_category(
        &bytes,
        |e| matches!(e, CodecError::UnknownContainerTag(9)),
        "unknown tag",
    );
}

#[test]
fn rejects_unsorted_keys() {
    let mut b = Builder::new();
    b.add(5, 1, array_payload(&[1, 2]), 2);
    b.add(5, 1, array_payload(&[3, 4]), 2); // 重复键
    let bytes = b.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| matches!(e, CodecError::KeysNotSortedUnique { .. }),
        "duplicate key",
    );

    let mut b2 = Builder::new();
    b2.add(9, 1, array_payload(&[1]), 1);
    b2.add(2, 1, array_payload(&[2]), 1); // 逆序
    let bytes = b2.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| matches!(e, CodecError::KeysNotSortedUnique { .. }),
        "descending key",
    );
}

#[test]
fn rejects_array_not_sorted_unique() {
    let mut b = Builder::new();
    b.add(1, 1, array_payload(&[1, 5, 3]), 3); // 未排序，card 仍声称 3
    let bytes = b.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| matches!(e, CodecError::ArrayNotSortedUnique { key: 1, index: 2 }),
        "array order",
    );

    let mut b2 = Builder::new();
    b2.add(1, 1, array_payload(&[4, 4]), 2); // 重复
    let bytes = b2.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| matches!(e, CodecError::ArrayNotSortedUnique { key: 1, index: 1 }),
        "array dup",
    );
}

#[test]
fn rejects_array_above_threshold() {
    // 4097 个元素却用 array 标签
    let lows: Vec<u16> = (0..=(ARRAY_MAX_CARDINALITY as u16)).collect();
    let mut b = Builder::new();
    b.add(1, 1, array_payload(&lows), lows.len() as u32);
    let bytes = b.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| {
            matches!(
                e,
                CodecError::ArrayExceedsThreshold {
                    key: 1,
                    cardinality: 4097
                }
            )
        },
        "array over threshold",
    );
}

#[test]
fn rejects_bitmap_below_threshold() {
    // 只有 10 个置位却声称 bitmap
    let payload = bitmap_payload(|low| [0usize, 1, 2, 3, 4, 5, 6, 7, 8, 9].contains(&low));
    let mut b = Builder::new();
    b.add(1, 2, payload, 10);
    let bytes = b.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| {
            matches!(
                e,
                CodecError::ArrayExceedsThreshold {
                    key: 1,
                    cardinality: 10
                }
            )
        },
        "bitmap under threshold",
    );
}

#[test]
fn rejects_cardinality_mismatch_array() {
    let mut b = Builder::new();
    b.add(1, 1, array_payload(&[1, 2, 3]), 4); // 实际 3，声称 4
    let bytes = b.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| {
            matches!(
                e,
                CodecError::CardinalityMismatch {
                    key: 1,
                    declared: 4,
                    actual: 3
                }
            )
        },
        "array card mismatch",
    );
}

#[test]
fn rejects_cardinality_mismatch_bitmap() {
    // 5000 置位，但声称 4999
    let payload = bitmap_payload(|low| (low * 7) % 3 != 0 && low < 16000);
    let actual = (0..16000usize).filter(|l| (l * 7) % 3 != 0).count() as u32;
    let mut b = Builder::new();
    b.add(1, 2, payload, actual - 1);
    let bytes = b.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| matches!(e, CodecError::CardinalityMismatch { key: 1, .. }),
        "bitmap card mismatch",
    );
}

#[test]
fn rejects_bad_payload_length() {
    // bitmap 负载长度不是 8192
    let mut b = Builder::new();
    let short = vec![0u8; 100];
    b.add(1, 2, short, 0);
    // 声明长度 100 会与期望 8192 不符（真实负载也只有 100，offset 校验随后触发，
    // 但长度检查在前）。
    let bytes = b.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| {
            matches!(
                e,
                CodecError::BadPayloadLength {
                    key: 1,
                    declared: 100,
                    ..
                }
            )
        },
        "bitmap short payload",
    );

    // array 奇数长度
    let mut b2 = Builder::new();
    b2.add(1, 1, vec![0u8; 5], 0);
    let bytes = b2.build_with(&[], |_| {});
    expect_category(
        &bytes,
        |e| {
            matches!(
                e,
                CodecError::BadPayloadLength {
                    key: 1,
                    declared: 5,
                    ..
                }
            )
        },
        "array odd payload",
    );
}

#[test]
fn rejects_bad_offset() {
    // 偏移不从目录末尾开始
    let mut b = Builder::new();
    b.add(1, 1, array_payload(&[1, 2]), 2);
    // 把 offset 改成 0（落在目录区）
    let bytes = b.build_with(&[(0, None, None, Some(0), None, None)], |_| {});
    expect_category(
        &bytes,
        |e| matches!(e, CodecError::BadOffset { key: 1, offset: 0 }),
        "offset into directory",
    );
}

#[test]
fn accepts_well_formed_builder_sample() {
    // 对照组：同样构造路径的合法文件必须成功
    let mut b = Builder::new();
    b.add(1, 1, array_payload(&[1, 2, 3]), 3);
    let lows: Vec<u16> = (0..5000).map(|i| (i * 13 % 60000) as u16).collect();
    let mut sorted = lows.clone();
    sorted.sort_unstable();
    sorted.dedup();
    let n = sorted.len();
    let payload = bitmap_payload(move |low| sorted.binary_search(&(low as u16)).is_ok());
    b.add(400, 2, payload, n as u32);
    let bytes = b.build_with(&[], |_| {});
    let s = codec::decode(&bytes).expect("well-formed must decode");
    assert_eq!(s.container_count(), 2);
}
