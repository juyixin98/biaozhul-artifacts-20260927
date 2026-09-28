//! Independent reference decompressor.
//!
//! This is a **second, independent implementation** of the LZ7B block
//! decompression algorithm, written from the format specification alone. It
//! shares no code, helpers, error types or parsers with
//! [`crate::core`]: its own byte reader, its own varint decoder, its own
//! header parser, its own (stringly-typed) error taxonomy with explicit
//! categories. The evidence suite cross-checks both implementations against
//! each other and against hand-authored fixtures; agreement is meaningful
//! precisely because neither produced the other's expected output.

/// Reference error categories, deliberately mirroring the four-way contract
/// but defined independently.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefErrorKind {
    Input,
    State,
    Resource,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RefError {
    pub kind: RefErrorKind,
    pub reason: String,
}

impl std::fmt::Display for RefError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}: {}", self.kind, self.reason)
    }
}

type RefResult<T> = std::result::Result<T, RefError>;

fn bad(reason: impl Into<String>) -> RefError {
    RefError {
        kind: RefErrorKind::Input,
        reason: reason.into(),
    }
}
fn state(reason: impl Into<String>) -> RefError {
    RefError {
        kind: RefErrorKind::State,
        reason: reason.into(),
    }
}
fn resource(reason: impl Into<String>) -> RefError {
    RefError {
        kind: RefErrorKind::Resource,
        reason: reason.into(),
    }
}

/// Frame byte as the spec defines it (0 independent, 1 dependent).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RefFrame {
    Independent,
    Dependent,
}

/// Parsed header, independent representation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RefHeader {
    pub frame: RefFrame,
    pub index: u32,
    pub prev_digest: u64,
    pub payload_crc: u32,
    pub declared_len: u64,
}

const WINDOW: usize = 4096;
const MIN_MATCH: u64 = 3;
const MAX_MATCH: u64 = 3 + 65535;
const MAX_OUT: u64 = 1024 * 1024;
const MAX_PAYLOAD: usize = 64 * 1024;
const MAX_RATIO: u64 = 200_000;
const MAGIC: &[u8; 4] = b"LZ7B";

fn be_u32(b: &[u8]) -> u32 {
    u32::from_be_bytes([b[0], b[1], b[2], b[3]])
}
fn be_u64(b: &[u8]) -> u64 {
    u64::from_be_bytes([b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]])
}

/// CRC-32/IEEE, re-derived from the polynomial (bit-at-a-time form).
fn crc32_ieee(data: &[u8]) -> u32 {
    let mut c: u32 = 0xFFFF_FFFF;
    for &x in data {
        c ^= u32::from(x);
        for _ in 0..8 {
            c = if c & 1 != 0 {
                (c >> 1) ^ 0xEDB8_8320
            } else {
                c >> 1
            };
        }
    }
    c ^ 0xFFFF_FFFF
}

/// Parse the fixed 30-byte header with the same *rules*, independently coded.
pub fn parse_header(raw: &[u8]) -> RefResult<RefHeader> {
    if raw.len() < 30 {
        return Err(bad(format!("block only {} bytes", raw.len())));
    }
    if &raw[0..4] != MAGIC {
        return Err(bad("magic mismatch"));
    }
    if raw[4] != 1 {
        return Err(bad(format!("version {} unsupported", raw[4])));
    }
    let frame = match raw[5] {
        0 => RefFrame::Independent,
        1 => RefFrame::Dependent,
        x => return Err(bad(format!("frame byte {x}"))),
    };
    let index = be_u32(&raw[6..10]);
    let prev_digest = be_u64(&raw[10..18]);
    let payload_crc = be_u32(&raw[18..22]);
    let declared_len = be_u64(&raw[22..30]);

    match frame {
        RefFrame::Independent if prev_digest != 0 => return Err(bad("indep digest nonzero")),
        RefFrame::Dependent if prev_digest == 0 => return Err(bad("dep digest zero")),
        _ => {}
    }
    if declared_len > MAX_OUT {
        return Err(resource("declared output above cap"));
    }
    let plen = raw.len() - 30;
    if plen > MAX_PAYLOAD {
        return Err(resource("payload above cap"));
    }
    if declared_len > MAX_RATIO.saturating_mul(plen as u64) {
        return Err(resource("declared expansion ratio above cap"));
    }
    Ok(RefHeader {
        frame,
        index,
        prev_digest,
        payload_crc,
        declared_len,
    })
}

/// Cursor over payload bytes with its own LEB128 implementation.
struct Cursor<'a> {
    b: &'a [u8],
    p: usize,
}

impl<'a> Cursor<'a> {
    fn u8(&mut self) -> RefResult<u8> {
        let v = *self
            .b
            .get(self.p)
            .ok_or_else(|| bad("truncated token stream"))?;
        self.p += 1;
        Ok(v)
    }
    fn leb(&mut self) -> RefResult<u64> {
        let mut acc: u64 = 0;
        let mut n = 0u32;
        let mut groups = 0u32;
        loop {
            let x = self.u8()?;
            groups += 1;
            if groups > 10 {
                return Err(bad("varint too long"));
            }
            if n == 63 && (x & 0x7f) > 1 {
                return Err(bad("varint overflow"));
            }
            acc |= u64::from(x & 0x7f) << n;
            if x & 0x80 == 0 {
                if groups > 1 && x & 0x7f == 0 {
                    return Err(bad("varint noncanonical"));
                }
                return Ok(acc);
            }
            n += 7;
        }
    }
    fn bytes(&mut self, k: usize) -> RefResult<&'a [u8]> {
        // Subtraction form: never computes `self.p + k`, which could wrap.
        if k > self.b.len() - self.p {
            return Err(bad("literal overruns payload"));
        }
        let s = self.p;
        self.p += k;
        Ok(&self.b[s..self.p])
    }
}

/// Decode a full raw block (header + payload) against `dict`.
///
/// This intentionally takes the *raw* block and performs every check itself,
/// so that it can be pointed at bytes that never went through the core at all.
pub fn decompress_raw(raw: &[u8], dict: &[u8]) -> RefResult<Vec<u8>> {
    let h = parse_header(raw)?;
    let payload = &raw[30..];
    if crc32_ieee(payload) != h.payload_crc {
        return Err(bad("payload crc mismatch"));
    }
    if h.frame == RefFrame::Independent && !dict.is_empty() {
        return Err(state("independent block given a dictionary"));
    }
    if dict.len() > WINDOW {
        return Err(bad("dictionary larger than window"));
    }

    let mut out: Vec<u8> = Vec::with_capacity(h.declared_len as usize);
    let mut cur = Cursor { b: payload, p: 0 };

    loop {
        let tag = cur.u8()?;
        match tag {
            0 => {
                let k64 = cur.leb()?;
                // Reject values that do not fit usize instead of silently
                // truncating on a 32-bit target, and bound against the
                // declared output length before touching the payload slice.
                if k64 > usize::MAX as u64 {
                    return Err(bad("literal length exceeds usize"));
                }
                if k64 > h.declared_len {
                    return Err(bad("literal run exceeds declared length"));
                }
                let lit = cur.bytes(k64 as usize)?;
                grow(&mut out, lit.len(), h.declared_len)?;
                out.extend_from_slice(lit);
            }
            1 => {
                let dist = cur.leb()?;
                let delta = cur.leb()?;
                let len = delta
                    .checked_add(MIN_MATCH)
                    .ok_or_else(|| bad("len overflow"))?;
                if dist == 0 {
                    return Err(bad("zero distance"));
                }
                if !(MIN_MATCH..=MAX_MATCH).contains(&len) {
                    return Err(bad("match length out of range"));
                }
                if dist > WINDOW as u64 {
                    return Err(bad(format!("distance {dist} exceeds window {WINDOW}")));
                }
                let have = (dict.len() + out.len()) as u64;
                if dist > have {
                    return Err(bad(format!("distance {dist} > history {have}")));
                }
                grow(&mut out, len as usize, h.declared_len)?;
                // Overlap-safe: one byte per iteration, reading the growing
                // (dict prefix + out) history just like the spec states.
                for _ in 0..len {
                    let src = (dict.len() + out.len()) as u64 - dist;
                    let v = if (src as usize) < dict.len() {
                        dict[src as usize]
                    } else {
                        out[(src as usize) - dict.len()]
                    };
                    out.push(v);
                }
            }
            2 => {
                if cur.p != payload.len() {
                    return Err(bad("trailing bytes after end token"));
                }
                break;
            }
            t => return Err(bad(format!("tag {t} unknown"))),
        }
    }

    if out.len() as u64 != h.declared_len {
        return Err(bad("declared length mismatch"));
    }
    Ok(out)
}

#[allow(clippy::ptr_arg)] // Vec keeps the call sites self-contained
fn grow(out: &mut Vec<u8>, extra: usize, declared: u64) -> RefResult<()> {
    let total = out.len() + extra;
    if total as u64 > MAX_OUT {
        return Err(resource("output exceeds cap"));
    }
    if total as u64 > declared {
        return Err(bad("output exceeds declared length"));
    }
    Ok(())
}
