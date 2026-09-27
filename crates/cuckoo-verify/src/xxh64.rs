//! 独立的 XXH64 参考实现（验证专用）。
//!
//! 这份代码是按 XXH64 规范（r44）**独立重写**的，不复用 `cuckoo-core` 使用的
//! xxhash-rust crate：验证器据此自行推导指纹与候选桶，使参考答案不依赖被测核心自身。
//! 正确性由官方公开的已知答案向量（seed=0）与第二份独立实现（xxhash-rust）的
//! 全量交叉比对共同锚定（见模块测试）。

// XXH64 官方质数（r44），逐位核对自 xxhash.h。
const PRIME64_1: u64 = 0x9E37_79B1_85EB_CA87;
const PRIME64_2: u64 = 0xC2B2_AE3D_27D4_EB4F;
const PRIME64_3: u64 = 0x1656_67B1_9E37_79F9;
const PRIME64_4: u64 = 0x85EB_CA77_C2B2_AE63;
const PRIME64_5: u64 = 0x27D4_EB2F_1656_67C5;
// 4 字节尾块乘的是 P1 的低 32 位。


#[inline]
fn round(acc: u64, lane: u64) -> u64 {
    acc.wrapping_add(lane.wrapping_mul(PRIME64_2))
        .rotate_left(31)
        .wrapping_mul(PRIME64_1)
}

#[inline]
fn merge_round(acc: u64, val: u64) -> u64 {
    acc ^ round(0, val).wrapping_mul(1) // round(0, val) 后并入
}

pub fn xxh64(input: &[u8], seed: u64) -> u64 {
    let len = input.len() as u64;
    let mut p = input;
    let mut h64: u64;

    if p.len() >= 32 {
        let mut v1 = seed
            .wrapping_add(PRIME64_1)
            .wrapping_add(PRIME64_2);
        let mut v2 = seed.wrapping_add(PRIME64_2);
        let mut v3 = seed;
        let mut v4 = seed.wrapping_sub(PRIME64_1);

        while p.len() >= 32 {
            v1 = round(v1, read_u64(&p[0..8]));
            v2 = round(v2, read_u64(&p[8..16]));
            v3 = round(v3, read_u64(&p[16..24]));
            v4 = round(v4, read_u64(&p[24..32]));
            p = &p[32..];
        }
        h64 = v1
            .rotate_left(1)
            .wrapping_add(v2.rotate_left(7))
            .wrapping_add(v3.rotate_left(12))
            .wrapping_add(v4.rotate_left(18));
        h64 = merge_acc(h64, v1);
        h64 = merge_acc(h64, v2);
        h64 = merge_acc(h64, v3);
        h64 = merge_acc(h64, v4);
    } else {
        h64 = seed.wrapping_add(PRIME64_5);
    }

    h64 = h64.wrapping_add(len);

    while p.len() >= 8 {
        let k1 = round(0, read_u64(&p[0..8]));
        h64 = (h64 ^ k1).rotate_left(27).wrapping_mul(PRIME64_1).wrapping_add(PRIME64_4);
        p = &p[8..];
    }
    if p.len() >= 4 {
        let k = read_u32(&p[0..4]) as u64;
        // 与 xxhash-rust/官方 C 一致：乘**完整 64 位 PRIME_1**（不要截断成 32 位）。
        h64 ^= k.wrapping_mul(PRIME64_1);
        h64 = h64
            .rotate_left(23)
            .wrapping_mul(PRIME64_2)
            .wrapping_add(PRIME64_3);
        p = &p[4..];
    }
    while !p.is_empty() {
        let k = p[0] as u64;
        // 注意：1..3 字节尾块乘的是**完整 64 位 P5**，不是 32 位截断值
        // （这一点容易抄错，由全量交叉比对锁定）。
        h64 ^= k.wrapping_mul(PRIME64_5);
        h64 = h64.rotate_left(11).wrapping_mul(PRIME64_1);
        p = &p[1..];
    }

    avalanche(h64)
}

#[inline]
fn merge_acc(acc: u64, v: u64) -> u64 {
    let acc = acc ^ round(0, v);
    acc.wrapping_mul(PRIME64_1).wrapping_add(PRIME64_4)
}

#[inline]
fn avalanche(mut h: u64) -> u64 {
    h ^= h >> 33;
    h = h.wrapping_mul(PRIME64_2);
    h ^= h >> 29;
    h = h.wrapping_mul(PRIME64_3);
    h ^= h >> 32;
    h
}

#[inline]
fn read_u64(b: &[u8]) -> u64 {
    u64::from_le_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}
#[inline]
fn read_u32(b: &[u8]) -> u32 {
    u32::from_le_bytes([b[0], b[1], b[2], b[3]])
}

// merge_round 未使用，避免死代码警告。
#[allow(dead_code)]
fn _unused() {
    let _ = merge_round;
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 公开的 seed=0 KAT 锚点：空串 XXH64("",0)。
    ///
    /// 该值在实现前已由常数算术手工独立推导（P5 起步 + avalanche），
    /// 不来自被测 crate；再叠加下方对第二份实现的 1500+ 向量全量交叉比对。
    #[test]
    fn known_answer_vectors_seed_zero() {
        assert_eq!(xxh64(b"", 0), 0xEF46_DB37_51D8_E999);
    }

    #[test]
    fn cross_check_against_xxhash_rust() {
        // 验证器自有实现 vs 另一份互不相关的第三方实现：
        // 覆盖长度 0..=300（跨越 4/8/16/32 字节各边界）、多 seed、确定性伪随机内容。
        let mut lcg = 0x1234_5678u64;
        let seeds = [0u64, 1, 0xDEAD_BEEF, 0x434B_465F_4131_0001, u64::MAX];
        for &seed in &seeds {
            for n in 0..=300usize {
                let mut buf = Vec::with_capacity(n);
                for _ in 0..n {
                    lcg = lcg.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                    buf.push((lcg >> 33) as u8);
                }
                assert_eq!(
                    xxh64(&buf, seed),
                    xxhash_rust::xxh64::xxh64(&buf, seed),
                    "n={n} seed={seed:#x}"
                );
            }
        }
    }
}
