//! Minimal standard-alphabet base64 (with padding), enough for JSON bodies.
//! Written locally to avoid pulling another crate into the kernel build.

use crate::core::error::{Code, Error, Result};

const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

#[must_use]
pub fn encode(data: &[u8]) -> String {
    let mut out = String::with_capacity(data.len().div_ceil(3) * 4);
    for chunk in data.chunks(3) {
        let b0 = u32::from(chunk[0]);
        let b1 = chunk.get(1).copied().map(u32::from);
        let b2 = chunk.get(2).copied().map(u32::from);
        let triple = (b0 << 16) | (b1.unwrap_or(0) << 8) | b2.unwrap_or(0);
        out.push(ALPHABET[((triple >> 18) & 63) as usize] as char);
        out.push(ALPHABET[((triple >> 12) & 63) as usize] as char);
        match chunk.len() {
            3 => {
                out.push(ALPHABET[((triple >> 6) & 63) as usize] as char);
                out.push(ALPHABET[(triple & 63) as usize] as char);
            }
            2 => out.push(ALPHABET[((triple >> 6) & 63) as usize] as char),
            1 => {}
            _ => unreachable!(),
        }
        if chunk.len() < 3 {
            out.push('=');
        }
        if chunk.len() < 2 {
            out.push('=');
        }
    }
    out
}

pub fn decode(s: &str) -> Result<Vec<u8>> {
    let bytes = s.as_bytes();
    if bytes.is_empty() {
        return Ok(Vec::new());
    }
    if bytes.len() % 4 != 0 {
        return Err(Error::new(
            Code::BadString,
            "base64 length must be a multiple of 4",
        ));
    }

    let sextet = |c: u8| -> Result<u8> {
        match c {
            b'A'..=b'Z' => Ok(c - b'A'),
            b'a'..=b'z' => Ok(c - b'a' + 26),
            b'0'..=b'9' => Ok(c - b'0' + 52),
            b'+' => Ok(62),
            b'/' => Ok(63),
            b'=' => Ok(64),
            _ => Err(Error::new(Code::BadString, "invalid base64 character")),
        }
    };

    let quad_count = bytes.len() / 4;
    let mut out = Vec::with_capacity(quad_count * 3);

    for (qi, quad) in bytes.chunks_exact(4).enumerate() {
        let is_last = qi + 1 == quad_count;
        let v = [
            sextet(quad[0])?,
            sextet(quad[1])?,
            sextet(quad[2])?,
            sextet(quad[3])?,
        ];

        // Padding may occur only in the final quad and only in the two
        // canonical patterns "xxx=" or "xx==".
        match (v[0] == 64, v[1] == 64, v[2] == 64, v[3] == 64) {
            (false, false, false, false) => {
                // full 3 bytes
                out.push((v[0] << 2) | (v[1] >> 4));
                out.push((v[1] << 4) | (v[2] >> 2));
                out.push((v[2] << 6) | v[3]);
            }
            (false, false, false, true) if is_last => {
                // 2 bytes; the two unused low bits of v[2] must be zero
                if v[2] & 0x03 != 0 {
                    return Err(Error::new(
                        Code::BadString,
                        "non-canonical base64 trailing bits",
                    ));
                }
                out.push((v[0] << 2) | (v[1] >> 4));
                out.push((v[1] << 4) | (v[2] >> 2));
            }
            (false, false, true, true) if is_last => {
                // 1 byte; the four unused low bits of v[1] must be zero
                if v[1] & 0x0f != 0 {
                    return Err(Error::new(
                        Code::BadString,
                        "non-canonical base64 trailing bits",
                    ));
                }
                out.push((v[0] << 2) | (v[1] >> 4));
            }
            (true, _, _, _) => {
                return Err(Error::new(
                    Code::BadString,
                    "padding in non-padding position",
                ));
            }
            _ => {
                return Err(if is_last {
                    Error::new(Code::BadString, "non-canonical base64 padding")
                } else {
                    Error::new(Code::BadString, "padding is only allowed at the end")
                });
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rfc4648_vectors() {
        assert_eq!(encode(b""), "");
        assert_eq!(encode(b"f"), "Zg==");
        assert_eq!(encode(b"fo"), "Zm8=");
        assert_eq!(encode(b"foo"), "Zm9v");
        assert_eq!(encode(b"foob"), "Zm9vYg==");
        assert_eq!(encode(b"fooba"), "Zm9vYmE=");
        assert_eq!(encode(b"foobar"), "Zm9vYmFy");
    }

    #[test]
    fn roundtrip_and_rejects() {
        for s in ["", "a", "ab", "abc", "abcd", "abcde", &"x".repeat(100)] {
            assert_eq!(decode(&encode(s.as_bytes())).unwrap(), s.as_bytes());
        }
        assert!(decode("Zg=").is_err());
        assert!(decode("!!!!").is_err());
    }

    #[test]
    fn rejects_noncanonical_padding_and_bits() {
        // '=' before the final quad
        assert!(decode("AAAAZg=A").is_err());
        // '=' in position 3 of a quad
        assert!(decode("Zg=A").is_err());
        // Padding in positions 1 or 2
        assert!(decode("=AAA").is_err());
        assert!(decode("A=AA").is_err());
        // Nonzero trailing bits: "ZR==" vs canonical "ZQ==" for 'e'
        assert_eq!(decode("ZQ==").unwrap(), b"e");
        assert!(decode("ZR==").is_err());
        // '=' in the third slot but nonzero low bits
        assert!(decode("Zm9=").is_err());
        // Empty padding-only
        assert!(decode("====").is_err());
    }
}
