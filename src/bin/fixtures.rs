//! Generate the bundled synthetic fixtures into `samples/`.
//!
//! Run with `cargo run --bin fm-make-fixtures`. Everything is deterministic
//! (fixed LCG seed), so re-running reproduces byte-identical files.
//!
//! Outputs:
//!   samples/repetitive.json  — high-repeat text (period-7 cycle)
//!   samples/zeros.json       — text dominated by binary zero bytes
//!   samples/mixed.json       — pseudo-random bytes incl. all 256 values
//!
//! Each file: { "description", "encoding": "b64", "text_b64", "patterns_b64" }.

use std::fs;
use std::path::PathBuf;

use base64::Engine;
use serde::Serialize;

#[derive(Serialize)]
struct Fixture {
    description: String,
    encoding: &'static str,
    text_b64: String,
    patterns_b64: Vec<String>,
}

/// Deterministic LCG (Numerical Recipes constants), no external RNG.
struct Lcg(u64);
impl Lcg {
    fn next_u32(&mut self) -> u32 {
        self.0 = self
            .0
            .wrapping_mul(6_364_136_223)
            .wrapping_add(1_442_695_049);
        (self.0 >> 33) as u32
    }
}

fn b64(bytes: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

fn repetitive() -> Fixture {
    // Period-7 cycle repeated, with a deliberate inserted run of 'a'.
    let cycle = b"ABCABCD";
    let mut text = Vec::new();
    for _ in 0..400 {
        text.extend_from_slice(cycle);
    }
    text.extend_from_slice(b"aaaaaaaaaaaaaaaa");
    text.extend_from_slice(b"ABCABCD");
    let patterns: Vec<Vec<u8>> = vec![
        b"ABCABCD".to_vec(),    // the period — many non-overlapping hits
        b"ABCABCABCD".to_vec(), // spans a period boundary
        b"aaaa".to_vec(),       // overlaps inside the inserted run
        b"XYZ".to_vec(),        // absent
        b"".to_vec(),           // empty pattern
    ];
    Fixture {
        description: "High-repetition text: period-7 cycle (400x) + run of a's".into(),
        encoding: "b64",
        text_b64: b64(&text),
        patterns_b64: patterns.iter().map(|p| b64(p)).collect(),
    }
}

fn zeros() -> Fixture {
    // Zero-dominated text with sparse nonzero markers, including a \xFF.
    let mut text = vec![0u8; 600];
    let mut i = 0;
    while i < 600 {
        text[i] = 1;
        i += 37;
    }
    text[599] = 0xFF;
    text[0] = 0xFF;
    let patterns: Vec<Vec<u8>> = vec![
        vec![0x00],             // every zero position
        vec![0x00, 0x00],       // overlapping zero pairs
        vec![0x00, 0x01, 0x00], // around a sparse marker
        vec![0xFF],
        vec![0xFF, 0x00],
        vec![0x02], // absent byte
    ];
    Fixture {
        description: "Binary text dominated by zero bytes with sparse markers and 0xFF ends".into(),
        encoding: "b64",
        text_b64: b64(&text),
        patterns_b64: patterns.iter().map(|p| b64(p)).collect(),
    }
}

fn mixed() -> Fixture {
    // Pseudo-random bytes with every byte value guaranteed present, plus a
    // fixed embedded "needle" searched below.
    let mut rng = Lcg(0x1234_5678_9abc_def0);
    let mut text: Vec<u8> = (0..2000).map(|_| rng.next_u32() as u8).collect();
    // guarantee full alphabet coverage
    for (i, b) in (0u16..256).enumerate() {
        text[1000 + i] = b as u8;
    }
    let needle = b"NEEDLE-FM-0123456789";
    text.splice(300..300 + needle.len(), needle.iter().copied());
    let mut long = needle.to_vec();
    long.extend_from_slice(b"TAIL");
    let patterns: Vec<Vec<u8>> = vec![
        needle.to_vec(),
        long, // needle+TAIL — absent across boundary
        vec![b'N', b'E', b'E'],
        (0..=255u16).map(|i| i as u8).collect(), // full-alphabet window
        vec![0xDE, 0xAD, 0xBE, 0xEF],
        b"".to_vec(),
    ];
    Fixture {
        description: "Pseudo-random bytes covering all 256 values with an embedded needle".into(),
        encoding: "b64",
        text_b64: b64(&text),
        patterns_b64: patterns.iter().map(|p| b64(p)).collect(),
    }
}

// Fixture set: three synthetic byte texts with deterministic content.

fn main() -> anyhow::Result<()> {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("samples");
    fs::create_dir_all(&dir)?;
    for (name, fixture) in [
        ("repetitive.json", repetitive()),
        ("zeros.json", zeros()),
        ("mixed.json", mixed()),
    ] {
        let path = dir.join(name);
        let json = serde_json::to_string_pretty(&fixture)?;
        fs::write(&path, json)?;
        println!(
            "wrote {} ({} pattern{})",
            path.display(),
            fixture.patterns_b64.len(),
            if fixture.patterns_b64.len() == 1 {
                ""
            } else {
                "s"
            }
        );
    }
    Ok(())
}
