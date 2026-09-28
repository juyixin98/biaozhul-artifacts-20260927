//! Encoder and decoder for the version-1 binary format.

use std::io::{Read, Write};

use hbs_core::container::{Array, BITMAP_WORDS, Bitmap};
use hbs_core::{Container, HierBitmap};

use crate::crc32::Crc32;
use crate::error::{CardinalityError, FormatError};

/// Magic bytes at the start of every file.
pub const FORMAT_MAGIC: [u8; 4] = *b"HBS1";
/// Only format version this build produces or accepts.
pub const FORMAT_VERSION: u16 = 1;
/// Fixed header length: 24 bytes of fields followed by the 4-byte checksum.
pub const HEADER_LEN: usize = 28;
/// Size of one directory entry.
pub const DIR_ENTRY_LEN: usize = 16;

/// Container kind tag: sparse sorted array.
pub const KIND_ARRAY: u16 = 1;
/// Container kind tag: dense bitmap.
pub const KIND_BITMAP: u16 = 2;

/// Which physical container a directory entry describes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ContainerKind {
    /// Sparse array.
    Array,
    /// Dense bitmap.
    Bitmap,
}

/// Result of a successful decode.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DecodedFile {
    /// The decoded set.
    pub set: HierBitmap,
    /// Number of chunks declared in the directory.
    pub num_chunks: u32,
    /// Total number of bytes the encoded file occupies.
    pub encoded_len: usize,
    /// Number of sparse containers decoded.
    pub array_containers: usize,
    /// Number of dense containers decoded.
    pub bitmap_containers: usize,
}

// ---------------------------------------------------------------------------
// Encoding
// ---------------------------------------------------------------------------

/// Encode a set to a freshly allocated byte vector.
pub fn encode(set: &HierBitmap) -> Vec<u8> {
    let mut buf = Vec::new();
    encode_to(set, &mut buf).expect("writing to a Vec cannot fail");
    buf
}

/// Encode a set into any [`Write`]. Returns the number of bytes written.
pub fn encode_to(set: &HierBitmap, out: &mut impl Write) -> std::io::Result<usize> {
    let chunks: Vec<(u16, &Container)> = set.chunks().collect();
    let num_chunks = chunks.len() as u32;

    // Build payload regions in chunk order.
    let mut data: Vec<u8> = Vec::new();
    let mut entries: Vec<(u16, u16, u32, u32)> = Vec::with_capacity(chunks.len());
    for (key, c) in &chunks {
        let offset = data.len() as u32;
        let (kind, card) = match c {
            Container::Array(a) => {
                for &v in a.values() {
                    data.extend_from_slice(&v.to_le_bytes());
                }
                (KIND_ARRAY, a.len() as u32)
            }
            Container::Bitmap(b) => {
                for w in b.words() {
                    data.extend_from_slice(&w.to_le_bytes());
                }
                (KIND_BITMAP, b.len() as u32)
            }
        };
        entries.push((*key, kind, card, offset));
    }

    let dir_len = (chunks.len() * DIR_ENTRY_LEN) as u32;
    let data_len = data.len() as u32;

    let mut crc = Crc32::new();
    let mut head = Vec::with_capacity(HEADER_LEN);
    head.extend_from_slice(&FORMAT_MAGIC);
    head.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
    head.extend_from_slice(&0u16.to_le_bytes()); // flags
    head.extend_from_slice(&num_chunks.to_le_bytes());
    head.extend_from_slice(&(HEADER_LEN as u32).to_le_bytes());
    head.extend_from_slice(&dir_len.to_le_bytes());
    head.extend_from_slice(&data_len.to_le_bytes());
    debug_assert_eq!(head.len(), HEADER_LEN - 4);
    crc.update(&head);

    let mut directory = Vec::with_capacity(dir_len as usize);
    for (key, kind, card, offset) in &entries {
        directory.extend_from_slice(&key.to_le_bytes());
        directory.extend_from_slice(&kind.to_le_bytes());
        directory.extend_from_slice(&card.to_le_bytes());
        directory.extend_from_slice(&offset.to_le_bytes());
        directory.extend_from_slice(&0u32.to_le_bytes()); // reserved
    }
    crc.update(&directory);
    crc.update(&data);

    let mut written = 0usize;
    out.write_all(&head)?;
    written += head.len();
    out.write_all(&crc.finish().to_le_bytes())?;
    written += 4;
    out.write_all(&directory)?;
    written += directory.len();
    out.write_all(&data)?;
    written += data.len();
    Ok(written)
}

// ---------------------------------------------------------------------------
// Decoding
// ---------------------------------------------------------------------------

struct Reader<'a> {
    buf: &'a [u8],
    pos: usize,
}

impl<'a> Reader<'a> {
    fn new(buf: &'a [u8]) -> Self {
        Self { buf, pos: 0 }
    }

    fn take(&mut self, n: usize) -> Result<&'a [u8], FormatError> {
        let end = self.pos.checked_add(n).ok_or(FormatError::Truncated)?;
        if end > self.buf.len() {
            return Err(FormatError::Truncated);
        }
        let slice = &self.buf[self.pos..end];
        self.pos = end;
        Ok(slice)
    }

    fn u16(&mut self) -> Result<u16, FormatError> {
        Ok(u16::from_le_bytes(
            self.take(2)?
                .try_into()
                .map_err(|_| FormatError::Truncated)?,
        ))
    }

    fn u32(&mut self) -> Result<u32, FormatError> {
        Ok(u32::from_le_bytes(
            self.take(4)?
                .try_into()
                .map_err(|_| FormatError::Truncated)?,
        ))
    }

    fn u64(&mut self) -> Result<u64, FormatError> {
        Ok(u64::from_le_bytes(
            self.take(8)?
                .try_into()
                .map_err(|_| FormatError::Truncated)?,
        ))
    }
}

/// Decode an in-memory buffer with full validation.
pub fn decode(buf: &[u8]) -> Result<DecodedFile, FormatError> {
    // Magic is the first 4 bytes and identifies the format before anything
    // else: a non-HBS buffer is BadMagic even if it is shorter than a
    // complete header (but not shorter than the magic itself).
    if buf.len() < 4 {
        return Err(FormatError::Truncated);
    }
    if buf[..4] != FORMAT_MAGIC {
        return Err(FormatError::BadMagic);
    }
    if buf.len() < HEADER_LEN {
        return Err(FormatError::Truncated);
    }

    // --- header -----------------------------------------------------------
    let mut hr = Reader::new(&buf[..HEADER_LEN]);
    let mut magic = [0u8; 4];
    magic.copy_from_slice(hr.take(4)?);
    debug_assert_eq!(magic, FORMAT_MAGIC);
    let version = hr.u16()?;
    if version != FORMAT_VERSION {
        return Err(FormatError::UnsupportedVersion { found: version });
    }
    let flags = hr.u16()?;
    if flags != 0 {
        return Err(FormatError::BadFlags(flags));
    }
    let num_chunks = hr.u32()?;
    let dir_offset = hr.u32()?;
    let dir_len = hr.u32()?;
    let data_len = hr.u32()?;
    let stored_crc = u32::from_le_bytes(buf[24..28].try_into().unwrap());

    // Cross-check the header's own arithmetic before touching anything.
    if dir_offset != HEADER_LEN as u32 {
        return Err(FormatError::BadHeader(
            "directory offset must equal header length",
        ));
    }
    let expected_dir_len = (num_chunks as u64)
        .checked_mul(DIR_ENTRY_LEN as u64)
        .ok_or(FormatError::BadHeader("directory length overflow"))?;
    if dir_len as u64 != expected_dir_len {
        return Err(FormatError::BadHeader(
            "directory length != num_chunks * 16",
        ));
    }
    let data_start = dir_offset as u64 + dir_len as u64;
    let total = data_start
        .checked_add(data_len as u64)
        .ok_or(FormatError::BadHeader("declared file size overflow"))?;
    if total > buf.len() as u64 {
        return Err(FormatError::Truncated);
    }
    if total < buf.len() as u64 {
        return Err(FormatError::TrailingBytes);
    }

    // --- checksum (covers every byte except the 4 checksum bytes) ---------
    let mut crc = Crc32::new();
    crc.update(&buf[..24]);
    crc.update(&buf[HEADER_LEN..]);
    let computed = crc.finish();
    if computed != stored_crc {
        return Err(FormatError::ChecksumMismatch {
            stored: stored_crc,
            computed,
        });
    }

    // --- directory --------------------------------------------------------
    let data_region = &buf[data_start as usize..total as usize];
    let dir_region = &buf[dir_offset as usize..data_start as usize];
    let mut dr = Reader::new(dir_region);

    let mut set = HierBitmap::new();
    let mut arrays = 0usize;
    let mut bitmaps = 0usize;
    let mut prev_key: Option<u16> = None;
    let mut expected_offset: u32 = 0;

    for _ in 0..num_chunks {
        let key = dr.u16()?;
        let kind = dr.u16()?;
        let cardinality = dr.u32()? as usize;
        let offset = dr.u32()?;
        let reserved = dr.u32()?;
        if reserved != 0 {
            return Err(FormatError::BadDirectory("reserved field must be zero"));
        }

        // Keys strictly increasing -> unique, ordered chunks.
        if let Some(prev) = prev_key
            && key <= prev
        {
            return Err(FormatError::BadDirectory(
                "chunk keys not strictly increasing",
            ));
        }
        prev_key = Some(key);

        // Regions must appear in directory order, back to back.
        if offset != expected_offset {
            return Err(FormatError::BadDirectory("data offset gap or overlap"));
        }

        let container = match kind {
            KIND_ARRAY => {
                arrays += 1;
                if cardinality == 0 {
                    return Err(FormatError::Cardinality {
                        chunk: key,
                        detail: CardinalityError::Empty,
                    });
                }
                if cardinality > hbs_core::THRESHOLD {
                    return Err(FormatError::Cardinality {
                        chunk: key,
                        detail: CardinalityError::ArrayTooDense {
                            declared: cardinality,
                        },
                    });
                }
                let region_len = cardinality.checked_mul(2).ok_or(FormatError::Truncated)?;
                if offset as u64 + region_len as u64 > data_len as u64 {
                    return Err(FormatError::Truncated);
                }
                expected_offset = offset + region_len as u32;
                let region = &data_region[offset as usize..offset as usize + region_len];
                let mut ar = Reader::new(region);
                let mut values = Vec::with_capacity(cardinality);
                for _ in 0..cardinality {
                    values.push(ar.u16()?);
                }
                Container::Array(
                    Array::from_trusted(values, cardinality).map_err(|e| match e {
                        hbs_core::CoreError::NotSortedUnique => {
                            FormatError::ArrayNotSorted { chunk: key }
                        }
                        hbs_core::CoreError::CardinalityMismatch { claimed, actual } => {
                            FormatError::Cardinality {
                                chunk: key,
                                detail: CardinalityError::Mismatch {
                                    declared: claimed,
                                    actual,
                                },
                            }
                        }
                        hbs_core::CoreError::OutOfBounds => {
                            FormatError::BadDirectory("value out of bounds")
                        }
                    })?,
                )
            }
            KIND_BITMAP => {
                bitmaps += 1;
                let region_len = BITMAP_WORDS * 8;
                if cardinality <= hbs_core::THRESHOLD {
                    return Err(FormatError::Cardinality {
                        chunk: key,
                        detail: CardinalityError::BitmapTooSparse {
                            declared: cardinality,
                        },
                    });
                }
                if cardinality > 65_536 {
                    return Err(FormatError::Cardinality {
                        chunk: key,
                        detail: CardinalityError::Mismatch {
                            declared: cardinality,
                            actual: 65_536,
                        },
                    });
                }
                if offset as u64 + region_len as u64 > data_len as u64 {
                    return Err(FormatError::Truncated);
                }
                expected_offset = offset + region_len as u32;
                let region = &data_region[offset as usize..offset as usize + region_len];
                let mut br = Reader::new(region);
                let mut words = [0u64; BITMAP_WORDS];
                for w in words.iter_mut() {
                    *w = br.u64()?;
                }
                Container::Bitmap(Bitmap::from_trusted(words, cardinality).map_err(
                    |e| match e {
                        hbs_core::CoreError::CardinalityMismatch { claimed, actual } => {
                            FormatError::Cardinality {
                                chunk: key,
                                detail: CardinalityError::Mismatch {
                                    declared: claimed,
                                    actual,
                                },
                            }
                        }
                        other => FormatError::BadDirectory(match other {
                            hbs_core::CoreError::NotSortedUnique => "unexpected array error",
                            hbs_core::CoreError::OutOfBounds => "value out of bounds",
                            hbs_core::CoreError::CardinalityMismatch { .. } => unreachable!(),
                        }),
                    },
                )?)
            }
            other => return Err(FormatError::UnknownContainerKind(other)),
        };

        set.insert_chunk(key, container);
    }

    // Every payload byte must belong to exactly one region.
    if expected_offset != data_len {
        return Err(FormatError::BadDirectory(
            "payload regions do not cover the data section",
        ));
    }

    Ok(DecodedFile {
        set,
        num_chunks,
        encoded_len: total as usize,
        array_containers: arrays,
        bitmap_containers: bitmaps,
    })
}

/// Stream a file from any [`Read`] (e.g. a [`std::fs::File`]): reads the
/// fixed header first, then exactly the directory and payload the header
/// declares.
pub fn decode_from(mut input: impl Read) -> Result<DecodedFile, FormatError> {
    // Read the 4-byte magic first so a short or foreign file is classified
    // correctly (BadMagic) regardless of total length.
    let mut magic = [0u8; 4];
    match input.read_exact(&mut magic) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => {
            return Err(FormatError::Truncated);
        }
        Err(_) => return Err(FormatError::Truncated),
    }
    if magic != FORMAT_MAGIC {
        return Err(FormatError::BadMagic);
    }

    // Read the rest of the fixed header.
    let mut header = vec![0u8; HEADER_LEN];
    header[..4].copy_from_slice(&magic);
    input
        .read_exact(&mut header[4..])
        .map_err(|_| FormatError::Truncated)?;
    let data_len = u32::from_le_bytes(header[20..24].try_into().unwrap()) as usize;
    let dir_len = u32::from_le_bytes(header[16..20].try_into().unwrap()) as usize;
    // Offset of the directory is validated in `decode`; the bytes remaining
    // after the header are exactly directory + payload.
    let _dir_offset = u32::from_le_bytes(header[12..16].try_into().unwrap());
    let mut buf = header;
    let mut rest = input.by_ref().take((dir_len + data_len) as u64);
    std::io::copy(&mut rest, &mut buf).map_err(|_| FormatError::Truncated)?;
    if buf.len() != HEADER_LEN + dir_len + data_len {
        return Err(FormatError::Truncated);
    }
    let decoded = decode(&buf)?;
    // A well-formed file ends exactly here; reject extra bytes.
    let mut extra = [0u8; 1];
    match input.read(&mut extra) {
        Ok(0) => Ok(decoded),
        Ok(_) => Err(FormatError::TrailingBytes),
        Err(_) => Err(FormatError::Truncated),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mixed_set() -> HierBitmap {
        let mut s = HierBitmap::new();
        // sparse chunk
        for v in [0u32, 1, 100, 65535] {
            s.insert(v);
        }
        // dense chunk (key 1)
        for v in 65536..65536 + 5000 {
            s.insert(v);
        }
        // far sparse chunk
        s.insert(u32::MAX);
        s
    }

    #[test]
    fn roundtrip_preserves_everything() {
        let s = mixed_set();
        let bytes = encode(&s);
        let d = decode(&bytes).unwrap();
        assert_eq!(d.num_chunks, 3);
        assert_eq!(d.array_containers, 2);
        assert_eq!(d.bitmap_containers, 1);
        assert_eq!(d.encoded_len, bytes.len());
        assert_eq!(d.set, s);
        assert_eq!(d.set.len(), s.len());
    }

    #[test]
    fn roundtrip_streaming_from_file_like_reader() {
        let s = mixed_set();
        let bytes = encode(&s);
        let d = decode_from(std::io::Cursor::new(bytes)).unwrap();
        assert_eq!(d.set, s);
    }

    #[test]
    fn empty_set_roundtrips_to_header_only() {
        let s = HierBitmap::new();
        let bytes = encode(&s);
        assert_eq!(bytes.len(), HEADER_LEN);
        let d = decode(&bytes).unwrap();
        assert_eq!(d.num_chunks, 0);
        assert!(d.set.is_empty());
    }

    #[test]
    fn rejects_each_failure_class() {
        let s = mixed_set();
        let good = encode(&s);

        // bad magic
        let mut b = good.clone();
        b[0] ^= 0xFF;
        assert_eq!(decode(&b).unwrap_err(), FormatError::BadMagic);

        // unsupported version
        let mut b = good.clone();
        b[4..6].copy_from_slice(&999u16.to_le_bytes());
        // recompute crc so only version is wrong
        let crc = {
            let mut c = Crc32::new();
            c.update(&b[..24]);
            c.update(&b[HEADER_LEN..]);
            c.finish()
        };
        b[24..28].copy_from_slice(&crc.to_le_bytes());
        assert!(matches!(
            decode(&b).unwrap_err(),
            FormatError::UnsupportedVersion { found: 999 }
        ));

        // checksum mismatch: flip a payload byte only
        let mut b = good.clone();
        let last = b.len() - 1;
        b[last] ^= 0x01;
        assert!(matches!(
            decode(&b).unwrap_err(),
            FormatError::ChecksumMismatch { .. }
        ));

        // truncated: drop 10 bytes
        assert_eq!(
            decode(&good[..good.len() - 10]),
            Err(FormatError::Truncated)
        );

        // trailing bytes
        let mut b = good.clone();
        b.push(0);
        assert_eq!(decode(&b), Err(FormatError::TrailingBytes));

        // bad flags
        let mut b = good.clone();
        b[6..8].copy_from_slice(&1u16.to_le_bytes());
        // leave stale crc -> checksum fires first unless refreshed; refresh
        // so we exercise the flag check specifically.
        let crc = {
            let mut c = Crc32::new();
            c.update(&b[..24]);
            c.update(&b[HEADER_LEN..]);
            c.finish()
        };
        b[24..28].copy_from_slice(&crc.to_le_bytes());
        assert_eq!(decode(&b).unwrap_err(), FormatError::BadFlags(1));
    }

    #[test]
    fn rejects_bad_directory_order() {
        // Craft a header claiming 1 chunk but swap entry keys via two-chunk
        // file: duplicate the first entry to break strict increase.
        let mut s = HierBitmap::new();
        s.insert(1);
        s.insert(100_000);
        let mut b = encode(&s);
        // Directory starts at HEADER_LEN; overwrite the second entry's key
        // with the first entry's key.
        b[HEADER_LEN + DIR_ENTRY_LEN..HEADER_LEN + DIR_ENTRY_LEN + 2]
            .copy_from_slice(&0u16.to_le_bytes());
        // stale crc => checksum fires first; recompute to reach directory.
        let crc = {
            let mut c = Crc32::new();
            c.update(&b[..24]);
            c.update(&b[HEADER_LEN..]);
            c.finish()
        };
        b[24..28].copy_from_slice(&crc.to_le_bytes());
        assert!(matches!(
            decode(&b).unwrap_err(),
            FormatError::BadDirectory(_)
        ));
    }

    #[test]
    fn rejects_cardinality_lies() {
        let s = mixed_set();
        let mut b = encode(&s);
        // First directory entry is an array (chunk 0, card 4). Inflate its
        // declared cardinality above the sparse threshold; the decoder must
        // reject the claim before reading that many values.
        b[HEADER_LEN + 4..HEADER_LEN + 8].copy_from_slice(&5000u32.to_le_bytes());
        let crc = {
            let mut c = Crc32::new();
            c.update(&b[..24]);
            c.update(&b[HEADER_LEN..]);
            c.finish()
        };
        b[24..28].copy_from_slice(&crc.to_le_bytes());
        let err = decode(&b).unwrap_err();
        assert!(
            matches!(err, FormatError::Cardinality { chunk: 0, .. }),
            "got: {err:?}"
        );
    }

    #[test]
    fn rejects_unknown_container_kind() {
        let s = mixed_set();
        let mut b = encode(&s);
        b[HEADER_LEN + 2..HEADER_LEN + 4].copy_from_slice(&9u16.to_le_bytes());
        let crc = {
            let mut c = Crc32::new();
            c.update(&b[..24]);
            c.update(&b[HEADER_LEN..]);
            c.finish()
        };
        b[24..28].copy_from_slice(&crc.to_le_bytes());
        assert_eq!(
            decode(&b).unwrap_err(),
            FormatError::UnknownContainerKind(9)
        );
    }
}
