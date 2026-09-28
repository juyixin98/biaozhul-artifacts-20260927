//! Wire format: fixed 30-byte block header + LEB128 token payload.
//!
//! ```text
//! offset  size  field
//! 0       4     magic  b"LZ7B"
//! 4       1     version (1)
//! 5       1     frame type (0=independent, 1=dependent)
//! 6       4     block index, big-endian (0-based within a stream)
//! 10      8     predecessor dictionary digest, big-endian (0 iff independent)
//! 18      4     CRC-32 of payload, big-endian
//! 22      8     declared decompressed length, big-endian
//! ```
//!
//! Payload tokens:
//! * `0x00` varint run_len, then run_len raw literal bytes
//! * `0x01` varint distance, varint (match_len - MIN_MATCH)
//! * `0x02` end of stream (exactly one, must be the final byte)

use crate::core::checksum::crc32;
use crate::core::constants::{
    FORMAT_VERSION, HEADER_LEN, MAGIC, MAX_EXPANSION, MAX_MATCH, MAX_OUTPUT, MAX_PAYLOAD, MIN_MATCH,
};
use crate::core::error::{Code, Error, Result};
use crate::core::varint::Reader;

/// Whether a block carries its own dictionary or chains the previous one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FrameType {
    /// Dictionary starts empty; predecessor digest must be 0.
    Independent = 0,
    /// Dictionary is the window tail of the preceding block(s).
    Dependent = 1,
}

impl FrameType {
    pub fn from_u8(v: u8) -> Result<Self> {
        match v {
            0 => Ok(FrameType::Independent),
            1 => Ok(FrameType::Dependent),
            other => Err(Error::new(
                Code::BadFrameType,
                format!("frame type {other} is not 0 or 1"),
            )),
        }
    }

    pub fn is_dependent(self) -> bool {
        self == FrameType::Dependent
    }
}

/// Parsed fixed header.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BlockHeader {
    pub frame_type: FrameType,
    pub index: u32,
    pub prev_digest: u64,
    pub payload_crc: u32,
    pub decompressed_len: u64,
}

impl BlockHeader {
    pub fn encode(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&MAGIC);
        out.push(FORMAT_VERSION);
        out.push(self.frame_type as u8);
        out.extend_from_slice(&self.index.to_be_bytes());
        out.extend_from_slice(&self.prev_digest.to_be_bytes());
        out.extend_from_slice(&self.payload_crc.to_be_bytes());
        out.extend_from_slice(&self.decompressed_len.to_be_bytes());
    }

    /// Parse and fully validate a header from the start of `raw`.
    pub fn decode(raw: &[u8]) -> Result<Self> {
        if raw.len() < HEADER_LEN {
            return Err(Error::new(
                Code::BadLength,
                format!(
                    "block shorter than {HEADER_LEN}-byte header: {} bytes",
                    raw.len()
                ),
            ));
        }
        if raw[0..4] != MAGIC {
            return Err(Error::new(Code::BadMagic, "block does not begin with LZ7B"));
        }
        let version = raw[4];
        if version != FORMAT_VERSION {
            return Err(Error::new(
                Code::BadVersion,
                format!("version {version}, supported version is {FORMAT_VERSION}"),
            ));
        }
        let frame_type = FrameType::from_u8(raw[5])?;
        let index = u32::from_be_bytes(raw[6..10].try_into().unwrap());
        let prev_digest = u64::from_be_bytes(raw[10..18].try_into().unwrap());
        let payload_crc = u32::from_be_bytes(raw[18..22].try_into().unwrap());
        let decompressed_len = u64::from_be_bytes(raw[22..30].try_into().unwrap());

        match frame_type {
            FrameType::Independent if prev_digest != 0 => {
                return Err(Error::new(
                    Code::BadLength,
                    "independent block must carry zero predecessor digest",
                ));
            }
            FrameType::Dependent if prev_digest == 0 => {
                return Err(Error::new(
                    Code::BadLength,
                    "dependent block must carry a nonzero predecessor digest",
                ));
            }
            _ => {}
        }
        if decompressed_len > MAX_OUTPUT as u64 {
            return Err(Error::new(
                Code::OutputCapExceeded,
                format!("declared {decompressed_len} bytes exceeds per-block cap {MAX_OUTPUT}"),
            ));
        }
        let payload_len = raw.len() - HEADER_LEN;
        if payload_len > MAX_PAYLOAD {
            return Err(Error::new(
                Code::PayloadCapExceeded,
                format!("payload {payload_len} bytes exceeds cap {MAX_PAYLOAD}"),
            ));
        }
        // Expansion pre-check, performed before allocating output.
        if decompressed_len > (MAX_EXPANSION as u64) * payload_len as u64 {
            return Err(Error::new(
                Code::ExpansionCapExceeded,
                format!(
                    "declared expansion {:.1}x exceeds cap {MAX_EXPANSION}x",
                    decompressed_len as f64 / payload_len.max(1) as f64
                ),
            ));
        }
        Ok(BlockHeader {
            frame_type,
            index,
            prev_digest,
            payload_crc,
            decompressed_len,
        })
    }

    /// Payload slice of a raw block.
    pub fn payload<'a>(&self, raw: &'a [u8]) -> &'a [u8] {
        &raw[HEADER_LEN..]
    }

    /// Verify the header's payload CRC.
    pub fn verify_crc(&self, payload: &[u8]) -> Result<()> {
        let actual = crc32(payload);
        if actual != self.payload_crc {
            Err(Error::new(
                Code::CrcMismatch,
                format!(
                    "payload crc {actual:#010x} != header {:#010x}",
                    self.payload_crc
                ),
            ))
        } else {
            Ok(())
        }
    }
}

/// Token emitted by the parser.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Token {
    Literal(Vec<u8>),
    /// `(distance, length)` in bytes (length already offset by MIN_MATCH).
    Match {
        distance: usize,
        length: usize,
    },
    End,
}

/// Streaming token parser over a payload. Structural legality (tags, lengths,
/// ranges, single terminator, no trailing bytes) is enforced here; whether a
/// match distance reaches available output history is the decoder's job.
pub struct TokenParser<'a> {
    reader: Reader<'a>,
    finished: bool,
}

impl<'a> TokenParser<'a> {
    pub fn new(payload: &'a [u8]) -> Self {
        TokenParser {
            reader: Reader::new(payload),
            finished: false,
        }
    }

    pub fn next_token(&mut self) -> Result<Option<Token>> {
        if self.finished || self.reader.is_empty() {
            return Ok(None);
        }
        let pos = self.reader.position();
        let tag = self.reader.read_u8()?;
        match tag {
            0x00 => {
                let len = self.reader.read_usize()?;
                let data = self.reader.take(len)?.to_vec();
                Ok(Some(Token::Literal(data)))
            }
            0x01 => {
                let distance = self.reader.read_usize()?;
                let delta = self.reader.read_usize()?;
                let length = delta.checked_add(MIN_MATCH).ok_or_else(|| {
                    Error::new(Code::BadMatchLength, "match length delta overflow")
                })?;
                if distance == 0 {
                    return Err(Error::new(
                        Code::BadDistance,
                        format!("zero distance at {pos}"),
                    ));
                }
                if !(MIN_MATCH..=MAX_MATCH).contains(&length) {
                    return Err(Error::new(
                        Code::BadMatchLength,
                        format!("match length {length} at {pos} outside fixed range"),
                    ));
                }
                Ok(Some(Token::Match { distance, length }))
            }
            0x02 => {
                self.finished = true;
                if !self.reader.is_empty() {
                    return Err(Error::new(
                        Code::BadTokenStream,
                        format!("{} trailing byte(s) after END", self.reader.remaining()),
                    ));
                }
                Ok(Some(Token::End))
            }
            other => Err(Error::new(
                Code::BadTokenStream,
                format!("unknown token tag 0x{other:02x} at offset {pos}"),
            )),
        }
    }

    /// True after the terminating END token has been consumed.
    pub fn is_finished(&self) -> bool {
        self.finished
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core::varint;

    fn block(frame: FrameType, index: u32, prev: u64, payload: &[u8]) -> Vec<u8> {
        let mut raw = Vec::new();
        BlockHeader {
            frame_type: frame,
            index,
            prev_digest: prev,
            payload_crc: crc32(payload),
            decompressed_len: 0,
        }
        .encode(&mut raw);
        raw.extend_from_slice(payload);
        raw
    }

    #[test]
    fn header_roundtrip_and_caps() {
        let p = vec![0x02];
        let raw = block(FrameType::Independent, 7, 0, &p);
        let h = BlockHeader::decode(&raw).unwrap();
        assert_eq!(h.index, 7);
        assert_eq!(h.frame_type, FrameType::Independent);
        h.verify_crc(h.payload(&raw)).unwrap();
    }

    #[test]
    fn header_rejects_garbage() {
        let mut raw = block(FrameType::Independent, 0, 0, &[0x02]);
        raw[0] = b'X';
        assert_eq!(BlockHeader::decode(&raw).unwrap_err().code, Code::BadMagic);

        let mut raw = block(FrameType::Independent, 0, 0, &[0x02]);
        raw[4] = 99;
        assert_eq!(
            BlockHeader::decode(&raw).unwrap_err().code,
            Code::BadVersion
        );

        // independent + nonzero digest
        let raw = block(FrameType::Independent, 0, 42, &[0x02]);
        assert_eq!(BlockHeader::decode(&raw).unwrap_err().code, Code::BadLength);
        // dependent + zero digest
        let raw = block(FrameType::Dependent, 1, 0, &[0x02]);
        assert_eq!(BlockHeader::decode(&raw).unwrap_err().code, Code::BadLength);
    }

    #[test]
    fn expansion_cap_rejects_a_bomb_header() {
        // One-byte payload claiming huge (but <= MAX_OUTPUT) output.
        let raw = block_with_len(FrameType::Independent, 0, 0, &[0x02], MAX_OUTPUT as u64);
        assert_eq!(
            BlockHeader::decode(&raw).unwrap_err().code,
            Code::ExpansionCapExceeded
        );
    }

    fn block_with_len(
        frame: FrameType,
        index: u32,
        prev: u64,
        payload: &[u8],
        declared: u64,
    ) -> Vec<u8> {
        let mut raw = Vec::new();
        BlockHeader {
            frame_type: frame,
            index,
            prev_digest: prev,
            payload_crc: crc32(payload),
            decompressed_len: declared,
        }
        .encode(&mut raw);
        raw.extend_from_slice(payload);
        raw
    }

    #[test]
    fn token_stream_validates() {
        let mut p = Vec::new();
        p.push(0x00);
        varint::write(&mut p, 2);
        p.extend_from_slice(b"hi");
        p.push(0x01);
        varint::write(&mut p, 1);
        varint::write(&mut p, 0); // len MIN_MATCH
        p.push(0x02);

        let mut t = TokenParser::new(&p);
        assert_eq!(
            t.next_token().unwrap().unwrap(),
            Token::Literal(b"hi".to_vec())
        );
        assert_eq!(
            t.next_token().unwrap().unwrap(),
            Token::Match {
                distance: 1,
                length: MIN_MATCH
            }
        );
        assert_eq!(t.next_token().unwrap().unwrap(), Token::End);
        assert!(t.next_token().unwrap().is_none());

        // truncated literal
        let mut bad = Vec::new();
        bad.push(0x00);
        varint::write(&mut bad, 5);
        bad.extend_from_slice(b"ab");
        assert_eq!(
            TokenParser::new(&bad).next_token().unwrap_err().code,
            Code::BadTokenStream
        );

        // zero distance
        let mut bad = vec![0x01u8];
        varint::write(&mut bad, 0);
        varint::write(&mut bad, 0);
        assert_eq!(
            TokenParser::new(&bad).next_token().unwrap_err().code,
            Code::BadDistance
        );

        // trailing bytes after END
        assert_eq!(
            TokenParser::new(&[0x02, 0x00])
                .next_token()
                .unwrap_err()
                .code,
            Code::BadTokenStream
        );

        // unknown tag
        assert_eq!(
            TokenParser::new(&[0x09]).next_token().unwrap_err().code,
            Code::BadTokenStream
        );
    }
}
