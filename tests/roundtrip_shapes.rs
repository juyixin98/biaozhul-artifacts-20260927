//! End-to-end roundtrips for every required input shape, plus a bidirectional
//! cross-check against the independent Python reference implementation.

use rangecode::container::{decode_container, encode_adaptive, encode_static, Budgets};
use rangecode::table::FreqTable;

fn static_roundtrip(msg: &[u8]) {
    // A legal static table over 256 symbols with total <= bound: assign a
    // frequency proportional to occurrence, then normalize through the
    // adaptive model machinery if needed. Keep it simple with uniform for
    // short messages and adaptive for large ones (both exercise the kernel).
    if msg.is_empty() || msg.len() < 1000 {
        let table = FreqTable::uniform(256, 1 << 14).unwrap();
        let blob = encode_static(msg, &table, 256).unwrap();
        let p = decode_container(&blob, &Budgets::default()).unwrap();
        let out: Vec<u8> = p.symbols.iter().map(|s| *s as u8).collect();
        assert_eq!(out, msg);
        assert_eq!(p.declared_symbols as usize, msg.len());
    } else {
        adaptive_roundtrip(msg, 1 << 14, 0);
    }
}

fn adaptive_roundtrip(msg: &[u8], bound: u32, chunk: u32) {
    let blob = encode_adaptive(msg, 256, bound, chunk).unwrap();
    let p = decode_container(&blob, &Budgets::default()).unwrap();
    let out: Vec<u8> = p.symbols.iter().map(|s| *s as u8).collect();
    assert_eq!(out, msg);
    let mut cursor = 0u64;
    for c in &p.chunks {
        assert!((c.epoch as usize) < p.tables.len());
        cursor += c.symbols.len() as u64;
    }
    assert_eq!(cursor, msg.len() as u64);
}

#[test]
fn empty_input() {
    static_roundtrip(b"");
    adaptive_roundtrip(b"", 1 << 12, 0);
}

#[test]
fn extremely_short_inputs() {
    for b in 0u8..=16 {
        static_roundtrip(&[b]);
        adaptive_roundtrip(&[b], 1 << 12, 0);
    }
    static_roundtrip(&[0, 255]);
}

#[test]
fn long_repetitive_input() {
    let msg = vec![b'A'; 50_000];
    static_roundtrip(&msg);
    adaptive_roundtrip(&msg, 1 << 12, 256);
    let run_switch = {
        let mut v = vec![0u8; 20_000];
        v.extend(std::iter::repeat_n(255u8, 20_000));
        v
    };
    adaptive_roundtrip(&run_switch, 1 << 10, 128);
}

#[test]
fn alternating_input() {
    let msg: Vec<u8> = (0..20_000u32).map(|i| (i % 2) as u8).collect();
    static_roundtrip(&msg);
    adaptive_roundtrip(&msg, 1 << 12, 512);
}

#[test]
fn full_symbol_coverage() {
    let ascending: Vec<u8> = (0..=255u16).cycle().take(10_000).map(|x| x as u8).collect();
    let descending: Vec<u8> = (0..10_000u32).map(|i| (255 - (i % 256)) as u8).collect();
    let shuffled: Vec<u8> = (0..10_000u32)
        .map(|i| ((i.wrapping_mul(2_654_435_761) >> 24) & 0xFF) as u8)
        .collect();
    for m in [ascending, descending, shuffled] {
        static_roundtrip(&m);
        adaptive_roundtrip(&m, 1 << 12, 1000);
    }
}

#[test]
fn adaptive_mode_emits_multiple_epochs_under_small_bound() {
    let msg: Vec<u8> = (0..5_000u32).map(|i| (i % 7) as u8 + b'a').collect();
    let blob = encode_adaptive(&msg, 256, 1024, 0).unwrap();
    let p = decode_container(&blob, &Budgets::default()).unwrap();
    assert!(
        p.tables.len() > 1,
        "expected rescales, got {} epochs",
        p.tables.len()
    );
    let out: Vec<u8> = p.symbols.iter().map(|s| *s as u8).collect();
    assert_eq!(out, msg);
}

#[test]
fn chunks_are_independently_decodable_per_epoch() {
    let msg: Vec<u8> = (0..3_000u32).map(|i| (i % 5) as u8).collect();
    let blob = encode_adaptive(&msg, 256, 2048, 300).unwrap();
    let p = decode_container(&blob, &Budgets::default()).unwrap();
    for c in &p.chunks {
        assert!((c.epoch as usize) < p.tables.len());
        assert!(c.symbols.iter().all(|&s| s < 256));
    }
    assert!(p.chunks.len() > 3);
}

#[test]
fn compression_actually_shrinks_repetitive_data() {
    // A heavily skewed legal table (one dominant symbol) over 256 entries.
    let msg = vec![b'z'; 10_000];
    let mut freqs = vec![1u32; 256];
    freqs[b'z' as usize] = 10_000;
    let table = match FreqTable::new(&freqs, 1 << 14) {
        Ok(t) => t,
        // If total exceeds bound, scale the dominant weight to fit.
        Err(_) => {
            freqs[b'z' as usize] = 1 << 13;
            FreqTable::new(&freqs, 1 << 14).unwrap()
        }
    };
    let blob = encode_static(&msg, &table, 256).unwrap();
    // With dominant freq ~half the bound, each symbol costs ~1 bit: expect
    // roughly 1250 bytes of payload, far below 10k input.
    assert!(blob.len() < msg.len() / 4, "blob {}", blob.len());
}

// ---------------------------------------------------------------------------
// Bidirectional Python cross-check
// ---------------------------------------------------------------------------

use std::io::Write;
use std::process::{Command, Stdio};

fn python_available() -> bool {
    Command::new("python3")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Run a python snippet in the crate root, writing `stdin`, returning stdout.
fn python(script: &str, args: &[&str], stdin: &[u8]) -> Vec<u8> {
    let mut child = Command::new("python3")
        .arg("-c")
        .arg(script)
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .spawn()
        .expect("python3 spawn");
    let mut stdin_handle = child.stdin.take().unwrap();
    stdin_handle.write_all(stdin).unwrap();
    drop(stdin_handle);
    let out = child.wait_with_output().unwrap();
    assert!(
        out.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    out.stdout
}

const DECODE_SCRIPT: &str = r#"
import json, sys
sys.path.insert(0, "tools")
from reference_rangecoder import decode_vec
freqs = json.loads(sys.argv[1]); bound = int(sys.argv[2]); count = int(sys.argv[3])
data = bytes.fromhex(sys.stdin.read().strip())
out, _ = decode_vec(freqs, bound, data, count)
sys.stdout.write(json.dumps(out))
"#;

const ENCODE_SCRIPT: &str = r#"
import json, sys
sys.path.insert(0, "tools")
from reference_rangecoder import encode_vec
freqs = json.loads(sys.argv[1]); bound = int(sys.argv[2])
symbols = json.loads(sys.stdin.read())
blob, _ = encode_vec(freqs, bound, symbols)
sys.stdout.write(blob.hex())
"#;

#[test]
fn python_decodes_rust_payload() {
    if !python_available() {
        eprintln!("python3 unavailable — skipping cross-language test");
        return;
    }
    let table = FreqTable::new(&[1, 2, 3, 4], 256).unwrap();
    let symbols: Vec<u32> = (0..500u32).map(|i| i % 4).collect();
    let payload = rangecode::range::RangeEncoder::encode_vec(&table, &symbols).unwrap();
    let hex: String = payload.iter().map(|b| format!("{b:02x}")).collect();
    let out = python(
        DECODE_SCRIPT,
        &["[1,2,3,4]", "256", &symbols.len().to_string()],
        hex.as_bytes(),
    );
    let decoded: Vec<u32> = serde_json::from_slice(&out).unwrap();
    assert_eq!(decoded, symbols);
}

#[test]
fn rust_decodes_python_payload() {
    if !python_available() {
        eprintln!("python3 unavailable — skipping cross-language test");
        return;
    }
    let symbols: Vec<u32> = (0..777u32).map(|i| i % 4).collect();
    let stdin = serde_json::to_vec(&symbols).unwrap();
    let out = python(ENCODE_SCRIPT, &["[1,2,3,4]", "256"], &stdin);
    let hex = String::from_utf8(out).unwrap();
    let payload: Vec<u8> = (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect();
    let table = FreqTable::new(&[1, 2, 3, 4], 256).unwrap();
    let decoded = rangecode::range::decode_vec(&table, &payload, symbols.len()).unwrap();
    assert_eq!(decoded, symbols);
}
