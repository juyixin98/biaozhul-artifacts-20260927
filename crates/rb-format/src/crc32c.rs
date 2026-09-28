//! CRC32C（Castagnoli 多项式 0x1EDC6F41，反射输入/输出）。
//!
//! 自实现的表驱动版本（生成时构造一次 256 项查找表），避免引入外部依赖。
//! 该算法是标准 CRC32C（与 iSCSI / ext4 使用的一致），可用其它实现交叉验证。

use std::sync::OnceLock;

const POLY: u32 = 0x82F6_3B78; // 0x1EDC6F41 的位反转

fn table() -> &'static [u32; 256] {
    static TABLE: OnceLock<[u32; 256]> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut t = [0u32; 256];
        let mut i = 0u32;
        while i < 256 {
            let mut crc = i;
            let mut j = 0;
            while j < 8 {
                if crc & 1 != 0 {
                    crc = (crc >> 1) ^ POLY;
                } else {
                    crc >>= 1;
                }
                j += 1;
            }
            t[i as usize] = crc;
            i += 1;
        }
        t
    })
}

/// 增量计算：可传入前一段的 CRC（初始用 [`u32::MAX`]），最后再取反。
pub fn update(mut crc: u32, bytes: &[u8]) -> u32 {
    let t = table();
    for &b in bytes {
        crc = (crc >> 8) ^ t[((crc ^ b as u32) & 0xFF) as usize];
    }
    crc
}

/// 计算一段完整字节的 CRC32C（已做初始/最终异或）。
pub fn checksum(bytes: &[u8]) -> u32 {
    update(u32::MAX, bytes) ^ u32::MAX
}

#[cfg(test)]
mod tests {
    use super::{checksum, update};

    /// CRC32C 标准检查向量（与 Python 独立实现核对过）。
    #[test]
    fn known_vectors() {
        assert_eq!(checksum(b""), 0);
        assert_eq!(checksum(b"123456789"), 0xE306_9283);
        assert_eq!(checksum(b"hello world"), 0xC994_65AA);
    }

    #[test]
    fn incremental_matches_one_shot() {
        let data = b"the quick brown fox jumps over the lazy dog";
        let mut crc = u32::MAX;
        for chunk in data.chunks(7) {
            crc = update(crc, chunk);
        }
        assert_eq!(crc ^ u32::MAX, checksum(data));
    }
}
