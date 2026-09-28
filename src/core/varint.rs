//! Unsigned LEB128 used inside token streams.
//!
//! Strict decoder rules, each producing [`Code::BadVarint`](crate::core::error::Code::BadVarint):
//! * the stream may not end mid-byte;
//! * at most [`MAX_VARINT_LEN`] bytes;
//! * no non-canonical trailing group (the last group must carry a value bit
//!   when more than one byte is used), so each integer has one encoding.

use crate::core::error::{Code, Error, Result};

/// Bytes needed for any u64 LEB128.
pub const MAX_VARINT_LEN: usize = 10;

/// Append `value` as unsigned LEB128.
pub fn write(buf: &mut Vec<u8>, mut value: u64) {
    loop {
        let mut byte = (value & 0x7f) as u8;
        value >>= 7;
        if value != 0 {
            byte |= 0x80;
        }
        buf.push(byte);
        if value == 0 {
            break;
        }
    }
}

/// Cursor over a borrowed byte slice.
#[derive(Debug, Clone)]
pub struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    pub fn new(buf: &'a [u8]) -> Self {
        Reader { buf, pos: 0 }
    }

    pub fn position(&self) -> usize {
        self.pos
    }

    pub fn remaining(&self) -> usize {
        self.buf.len() - self.pos
    }

    pub fn is_empty(&self) -> bool {
        self.pos == self.buf.len()
    }

    /// Read one raw byte.
    pub fn read_u8(&mut self) -> Result<u8> {
        let b = *self.buf.get(self.pos).ok_or_else(|| {
            Error::new(
                Code::BadTokenStream,
                format!("unexpected end at offset {}", self.pos),
            )
        })?;
        self.pos += 1;
        Ok(b)
    }

    /// Borrow the next `n` bytes without copying, advancing the cursor.
    pub fn take(&mut self, n: usize) -> Result<&'a [u8]> {
        if self.remaining() < n {
            return Err(Error::new(
                Code::BadTokenStream,
                format!("need {n} bytes, {} remain", self.remaining()),
            ));
        }
        let start = self.pos;
        self.pos += n;
        Ok(&self.buf[start..self.pos])
    }

    /// Read an unsigned LEB128 value.
    pub fn read_varint(&mut self) -> Result<u64> {
        let mut result: u64 = 0;
        let mut shift = 0u32;
        let mut groups = 0usize;
        loop {
            let byte = self.read_u8()?;
            groups += 1;
            if groups > MAX_VARINT_LEN {
                return Err(Error::new(
                    Code::BadVarint,
                    format!("varint longer than {MAX_VARINT_LEN} bytes"),
                ));
            }
            let group = u64::from(byte & 0x7f);
            if shift == 63 {
                // Only a single value bit may remain.
                if group > 1 || (byte & 0x80) != 0 {
                    return Err(Error::new(Code::BadVarint, "u64 overflow in varint"));
                }
            }
            result |= group
                .checked_shl(shift)
                .ok_or_else(|| Error::new(Code::BadVarint, "varint shift overflow"))?;
            if byte & 0x80 == 0 {
                // Non-canonical check: with multiple groups, the final group
                // must contribute at least one bit.
                if groups > 1 && group == 0 {
                    return Err(Error::new(
                        Code::BadVarint,
                        "non-canonical trailing zero group",
                    ));
                }
                break;
            }
            shift += 7;
        }
        Ok(result)
    }

    /// Read a varint and require it to fit in `usize`.
    pub fn read_usize(&mut self) -> Result<usize> {
        let v = self.read_varint()?;
        usize::try_from(v)
            .map_err(|_| Error::new(Code::BadLength, format!("value {v} exceeds platform usize")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_boundaries() {
        for v in [
            0u64,
            1,
            127,
            128,
            16383,
            16384,
            u32::MAX as u64,
            u64::MAX,
            u64::MAX - 1,
        ] {
            let mut buf = Vec::new();
            write(&mut buf, v);
            assert!(buf.len() <= MAX_VARINT_LEN);
            assert_eq!(Reader::new(&buf).read_varint().unwrap(), v);
        }
    }

    #[test]
    fn rejects_truncated_overlong_and_overshift() {
        assert_eq!(
            Reader::new(&[0x80]).read_varint().unwrap_err().code,
            Code::BadTokenStream
        );
        // 11 continuation bytes.
        let long = [0x80u8; 11];
        assert_eq!(
            Reader::new(&long).read_varint().unwrap_err().code,
            Code::BadVarint
        );
        // u64 overflow: final group carrying 2 bits at shift 63.
        assert_eq!(
            Reader::new(&[0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x02])
                .read_varint()
                .unwrap_err()
                .code,
            Code::BadVarint
        );
        // Non-canonical trailing zero: 0x80 0x00 == 0 in two bytes.
        assert_eq!(
            Reader::new(&[0x80, 0x00]).read_varint().unwrap_err().code,
            Code::BadVarint
        );
    }
}
