//! MSB-first bit writer/reader used by the block body.
//!
//! Bit order within a byte is high-to-low (bit 7 first), matching DEFLATE
//! packed data. The last byte is padded with zero bits, which the decoder is
//! required to verify explicitly.

/// Packs code words MSB-first into a byte vector.
#[derive(Debug, Default, Clone)]
pub struct BitWriter {
    buf: Vec<u8>,
    current: u8,
    /// Number of valid bits already accumulated in `current` (0..=7).
    used: u8,
    total_bits: u64,
}

impl BitWriter {
    /// Create an empty writer with `capacity_hint` bytes reserved.
    pub fn with_capacity(capacity_hint: usize) -> Self {
        BitWriter {
            buf: Vec::with_capacity(capacity_hint),
            current: 0,
            used: 0,
            total_bits: 0,
        }
    }

    /// Append one bit (only bit 0 is taken from `bit`).
    pub fn write_bit(&mut self, bit: u8) {
        self.current |= (bit & 1) << (7 - self.used);
        self.used += 1;
        self.total_bits += 1;
        if self.used == 8 {
            self.buf.push(self.current);
            self.current = 0;
            self.used = 0;
        }
    }

    /// Append `len` bits of `code`, which must be supplied MSB-aligned
    /// (first transmitted bit is bit 31).
    pub fn write_bits(&mut self, code: u32, len: u8) {
        for i in 0..len {
            self.write_bit(((code >> (31 - i)) & 1) as u8);
        }
    }

    /// Finish and return `(bytes, total_content_bits)`. Remaining bits in the
    /// last byte are zero padding.
    pub fn finish(mut self) -> (Vec<u8>, u64) {
        if self.used > 0 {
            self.buf.push(self.current);
        }
        (self.buf, self.total_bits)
    }
}

/// Reads individual bits from a byte slice, bounded by a declared number of
/// *content* bits. Bits beyond that bound (the final-byte padding) are not
/// content and are inspected separately via [`BitReader::padding_is_zero`].
#[derive(Debug)]
pub struct BitReader<'a> {
    data: &'a [u8],
    /// Bits already consumed as content.
    consumed: u64,
    /// Number of content bits declared by the block header.
    content_bits: u64,
}

impl<'a> BitReader<'a> {
    /// Construct over `data`, with exactly `content_bits` valid content bits.
    /// Returns [`crate::error::ErrorKind::TruncatedBitstream`] up front when
    /// the byte slice cannot hold that many bits.
    pub fn new(data: &'a [u8], content_bits: u64) -> crate::error::Result<Self> {
        let available = (data.len() as u64).checked_mul(8).ok_or_else(|| {
            crate::error::Error::new(
                crate::error::ErrorKind::TruncatedBitstream,
                "body byte length overflows u64",
            )
        })?;
        if content_bits > available {
            return Err(crate::error::Error::new(
                crate::error::ErrorKind::TruncatedBitstream,
                format!(
                    "header declares {content_bits} content bits but body only holds {available} bits"
                ),
            ));
        }
        Ok(BitReader {
            data,
            consumed: 0,
            content_bits,
        })
    }

    /// Read the next content bit MSB-first.
    pub fn read_bit(&mut self) -> crate::error::Result<u8> {
        if self.consumed >= self.content_bits {
            return Err(crate::error::Error::new(
                crate::error::ErrorKind::TruncatedBitstream,
                format!("bitstream ended after {} content bits", self.consumed),
            ));
        }
        let byte_idx = (self.consumed >> 3) as usize;
        let bit_idx = (self.consumed & 7) as u8;
        let bit = (self.data[byte_idx] >> (7 - bit_idx)) & 1;
        self.consumed += 1;
        Ok(bit)
    }

    /// Number of content bits consumed so far.
    pub fn consumed(&self) -> u64 {
        self.consumed
    }

    /// Declared content bit budget.
    pub fn content_bits(&self) -> u64 {
        self.content_bits
    }

    /// Verify that all bits following the content region are zero padding and
    /// that no whole unused bytes trail the body (`body_len ==
    /// ceil(content_bits/8)`).
    pub fn verify_padding(&self) -> crate::error::Result<()> {
        let expected_bytes = self.content_bits.div_ceil(8) as usize;
        if self.data.len() != expected_bytes {
            return Err(crate::error::Error::new(
                crate::error::ErrorKind::TruncatedBitstream,
                format!(
                    "body has {} bytes but {} are needed for {} content bits",
                    self.data.len(),
                    expected_bytes,
                    self.content_bits
                ),
            ));
        }
        let mut bit = self.content_bits;
        let total = (self.data.len() as u64) * 8;
        while bit < total {
            let byte_idx = (bit >> 3) as usize;
            let bit_idx = (bit & 7) as u8;
            if (self.data[byte_idx] >> (7 - bit_idx)) & 1 != 0 {
                return Err(crate::error::Error::new(
                    crate::error::ErrorKind::InvalidTrailingBits,
                    format!("non-zero padding at bit {bit}"),
                ));
            }
            bit += 1;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_bits_with_padding() {
        let mut w = BitWriter::default();
        // code "10110"
        for &b in &[1u8, 0, 1, 1, 0] {
            w.write_bit(b);
        }
        let (bytes, bits) = w.finish();
        assert_eq!(bits, 5);
        assert_eq!(bytes, vec![0b1011_0000]);
        let mut r = BitReader::new(&bytes, bits).unwrap();
        let out: Vec<u8> = (0..5).map(|_| r.read_bit().unwrap()).collect();
        assert_eq!(out, vec![1, 0, 1, 1, 0]);
        r.verify_padding().unwrap();
    }

    #[test]
    fn rejects_nonzero_padding() {
        // 5 content bits, padding bits set to 100.
        let bytes = vec![0b1011_0100u8];
        let r = BitReader::new(&bytes, 5).unwrap();
        r.verify_padding().unwrap_err();
    }

    #[test]
    fn reads_past_content_budget_fail() {
        let bytes = vec![0xFF];
        let mut r = BitReader::new(&bytes, 1).unwrap();
        assert_eq!(r.read_bit().unwrap(), 1);
        assert_eq!(
            r.read_bit().unwrap_err().kind(),
            crate::error::ErrorKind::TruncatedBitstream
        );
    }

    #[test]
    fn declares_more_bits_than_bytes_hold() {
        let err = BitReader::new(&[0u8; 1], 9).unwrap_err();
        assert_eq!(err.kind(), crate::error::ErrorKind::TruncatedBitstream);
    }

    #[test]
    fn trailing_byte_rejected_even_when_zero() {
        // One content bit packed into one byte, but body carries two bytes.
        let bytes = vec![0b0000_0000, 0x00];
        let r = BitReader::new(&bytes, 1).unwrap();
        assert_eq!(
            r.verify_padding().unwrap_err().kind(),
            crate::error::ErrorKind::TruncatedBitstream
        );
    }
}
