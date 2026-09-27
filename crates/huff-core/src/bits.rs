//! MSB-first bit packing (see FORMAT.md §5).
//!
//! Bits are consumed and emitted from the most significant bit of each byte.
//! The writer buffers at most 7 unflushed bits, so every `write_bits` call
//! with `len <= 32` fits in a `u64` accumulator.

use crate::error::{HuffError, Result};

/// Byte-oriented, MSB-first bit writer.
#[derive(Debug, Default, Clone)]
pub struct BitWriter {
    /// Complete bytes emitted so far.
    bytes: Vec<u8>,
    /// Pending bits, left-aligned in the low `nbits` of the byte region.
    acc: u8,
    /// Number of valid bits in `acc` (0..8).
    nbits: u8,
}

impl BitWriter {
    /// Create an empty writer.
    pub fn new() -> Self {
        Self::default()
    }

    /// Number of payload bits written so far.
    pub fn bit_len(&self) -> u64 {
        self.bytes.len() as u64 * 8 + self.nbits as u64
    }

    /// Append `len` low bits of `value`, MSB of that run first.
    /// `len` must be in `0..=32`.
    pub fn write_bits(&mut self, value: u32, len: u8) {
        debug_assert!(len <= 32);
        let mut v = value as u64;
        let mut remaining = len;
        while remaining > 0 {
            let room = 8 - self.nbits;
            let take = remaining.min(room);
            // Shift the top `take` bits of the remaining run into place.
            let shift = remaining - take;
            let chunk = (v >> shift) as u8 & ((1u16 << take) - 1) as u8;
            self.acc |= chunk << (room - take);
            v &= (1u64 << shift) - 1; // drop consumed high bits
            remaining -= take;
            self.nbits += take;
            if self.nbits == 8 {
                self.bytes.push(self.acc);
                self.acc = 0;
                self.nbits = 0;
            }
        }
    }

    /// Flush the pending partial byte, padding the low bits with zeros.
    /// Returns the padded bytes; total bit count is unchanged in meaning
    /// (the caller must store the unpadded length separately).
    pub fn finish(mut self) -> (Vec<u8>, u64) {
        let bits = self.bit_len();
        if self.nbits > 0 {
            self.bytes.push(self.acc); // low padding bits are already zero
        }
        (self.bytes, bits)
    }
}

/// Bounded, MSB-first bit reader over a fixed slice.
#[derive(Debug, Clone)]
pub struct BitReader<'a> {
    data: &'a [u8],
    /// Next unconsumed bit index, counted from the MSB of byte 0.
    pos: u64,
    /// Total valid bits (may be smaller than `data.len() * 8` on the last byte).
    limit: u64,
}

impl<'a> BitReader<'a> {
    /// Reader over `data` limited to `limit_bits` valid bits.
    pub fn new(data: &'a [u8], limit_bits: u64) -> Result<Self> {
        let capacity = data.len() as u64 * 8;
        if limit_bits > capacity {
            return Err(HuffError::BitstreamTruncated);
        }
        Ok(Self { data, pos: 0, limit: limit_bits })
    }

    /// Valid bits remaining.
    pub fn remaining(&self) -> u64 {
        self.limit - self.pos
    }

    /// Read one bit.
    pub fn read_bit(&mut self) -> Result<u8> {
        if self.pos >= self.limit {
            return Err(HuffError::TruncatedCodeword);
        }
        let byte_index = (self.pos >> 3) as usize;
        let bit_index = self.pos & 7;
        let bit = (self.data[byte_index] >> (7 - bit_index)) & 1;
        self.pos += 1;
        Ok(bit)
    }

    /// Read `len` bits (`len <= 32`) into the low bits of the returned value.
    pub fn read_bits(&mut self, len: u8) -> Result<u32> {
        debug_assert!(len <= 32);
        if (self.pos + len as u64) > self.limit {
            return Err(HuffError::TruncatedCodeword);
        }
        let mut value: u32 = 0;
        for _ in 0..len {
            value = (value << 1) | self.read_bit()? as u32;
        }
        Ok(value)
    }

    /// After exactly the declared number of symbols has been decoded the
    /// stream must be consumed up to `limit`; any declared-but-unread bits
    /// are malformed (they belong to the zero-padded tail).
    pub fn expect_exhausted(&self) -> Result<()> {
        if self.pos == self.limit {
            Ok(())
        } else {
            Err(HuffError::InvalidPadding)
        }
    }
}

/// Verify that every padding bit of the final partial byte (the bits after
/// `valid_bits` up to the byte boundary) is zero.
pub fn check_zero_padding(body: &[u8], valid_bits: u64) -> Result<()> {
    let body_bytes = body.len() as u64;
    if valid_bits > body_bytes * 8 {
        return Err(HuffError::BitstreamTruncated);
    }
    let mut p = valid_bits;
    let end = (body_bytes * 8).min(valid_bits + 8); // only the tail byte matters
    while p < end {
        let byte = body[(p >> 3) as usize];
        let bit_index = p & 7;
        if byte & (1 << (7 - bit_index)) != 0 {
            return Err(HuffError::InvalidPadding);
        }
        p += 1;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn msb_first_roundtrip_mixed_widths() {
        let mut w = BitWriter::new();
        w.write_bits(0b101, 3);
        w.write_bits(0b0110, 4);
        w.write_bits(1, 1);
        // 8 bits total -> single byte 0b10101101
        let (bytes, bits) = w.finish();
        assert_eq!(bits, 8);
        assert_eq!(bytes, [0b1010_1101]);

        let mut r = BitReader::new(&bytes, bits).unwrap();
        assert_eq!(r.read_bits(3).unwrap(), 0b101);
        assert_eq!(r.read_bits(4).unwrap(), 0b0110);
        assert_eq!(r.read_bit().unwrap(), 1);
        r.expect_exhausted().unwrap();
        check_zero_padding(&bytes, bits).unwrap();
    }

    #[test]
    fn padding_zeros_and_bounds() {
        let mut w = BitWriter::new();
        w.write_bits(0b11001, 5); // 3 zero padding bits follow
        let (bytes, bits) = w.finish();
        assert_eq!(bytes, [0b1100_1000]);
        let mut r = BitReader::new(&bytes, bits).unwrap();
        assert_eq!(r.read_bits(5).unwrap(), 0b11001);
        r.expect_exhausted().unwrap();
        check_zero_padding(&bytes, bits).unwrap();

        // A set padding bit must be detected (valid bits are the low-index
        // 5 bits; flip padding bit at index 5, i.e. 0b11001_1_00).
        let bad = [0b1100_1100u8];
        assert_eq!(check_zero_padding(&bad, bits).unwrap_err(), HuffError::InvalidPadding);

        // Declaring more valid bits than the body holds is rejected.
        assert_eq!(
            BitReader::new(&bytes, 9).unwrap_err(),
            HuffError::BitstreamTruncated
        );
    }

    #[test]
    fn reads_past_end_are_truncated() {
        let mut w = BitWriter::new();
        w.write_bits(0b1, 1);
        let (bytes, bits) = w.finish();
        let mut r = BitReader::new(&bytes, bits).unwrap();
        assert_eq!(r.read_bits(3).unwrap_err(), HuffError::TruncatedCodeword);
    }
}
