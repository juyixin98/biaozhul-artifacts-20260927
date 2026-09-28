//! lz77b — command line driver for the codec kernel and reference checker.
//!
//! Subcommands:
//! * `compress   <in> <out.lzb>`                 one independent block
//! * `decode     <in.lzb> <out> [dict-file]`     core decoder
//! * `verify     <in.lzb> [dict-file]`           core AND reference, compares
//! * `chain      <in> <dir> <chunk-bytes>`       encode multi-block chain
//! * `unchain    <dir> <out>`                    decode a chain directory
//! * `hexdump    <in.lzb>`                       show header fields

use std::path::PathBuf;

use lz77b::core::constants::HEADER_LEN;
use lz77b::core::decoder::ChainSession;
use lz77b::core::encoder::encode_block;
use lz77b::core::format::{BlockHeader, FrameType};
use lz77b::reference;

fn read(p: &str) -> Vec<u8> {
    std::fs::read(p).unwrap_or_else(|e| panic!("read {p}: {e}"))
}
fn write(p: &str, b: &[u8]) {
    std::fs::write(p, b).unwrap_or_else(|e| panic!("write {p}: {e}"));
}

fn main() {
    let mut args = std::env::args().skip(1);
    let cmd = args.next().unwrap_or_else(|| usage());
    let args: Vec<String> = args.collect();
    match cmd.as_str() {
        "compress" => {
            assert_eq!(args.len(), 2, "compress <in> <out.lzb>");
            let input = read(&args[0]);
            let out = encode_block(FrameType::Independent, 0, &[], &input)
                .unwrap_or_else(|e| panic!("encode: {e}"));
            write(&args[1], &out.raw);
            println!(
                "compressed {} -> {} bytes, {} match tokens",
                input.len(),
                out.raw.len(),
                out.stats.match_tokens
            );
        }
        "decode" => {
            assert!(
                args.len() == 2 || args.len() == 3,
                "decode <in.lzb> <out> [dict]"
            );
            let raw = read(&args[0]);
            let dict = args.get(2).map(|d| read(d)).unwrap_or_default();
            let h = BlockHeader::decode(&raw).unwrap_or_else(|e| panic!("header: {e}"));
            let data = lz77b::core::decoder::decode_payload(
                h.frame_type,
                h.payload(&raw),
                &dict,
                h.decompressed_len,
            )
            .unwrap_or_else(|e| panic!("decode: {e}"));
            write(&args[1], &data);
            println!("decoded {} bytes -> {}", data.len(), args[1]);
        }
        "verify" => {
            assert!(args.len() == 1 || args.len() == 2, "verify <in.lzb> [dict]");
            let raw = read(&args[0]);
            let dict = args.get(1).map(|d| read(d)).unwrap_or_default();

            let h = BlockHeader::decode(&raw).unwrap_or_else(|e| panic!("core header: {e}"));
            let core = lz77b::core::decoder::decode_payload(
                h.frame_type,
                h.payload(&raw),
                &dict,
                h.decompressed_len,
            )
            .unwrap_or_else(|e| panic!("core decode: {e}"));
            let refe = reference::decompress_raw(&raw, &dict)
                .unwrap_or_else(|e| panic!("reference decode: {e}"));
            assert_eq!(core.len(), refe.len(), "length disagreement");
            assert_eq!(core, refe, "byte disagreement");
            println!(
                "OK: core and independent reference agree on {} bytes (frame={}, index={})",
                core.len(),
                h.frame_type as u8,
                h.index
            );
        }
        "chain" => {
            assert_eq!(args.len(), 3, "chain <in> <dir> <chunk-bytes>");
            let input = read(&args[0]);
            let chunk: usize = args[2].parse().expect("chunk bytes");
            std::fs::create_dir_all(&args[1]).expect("mkdir");
            let blocks = lz77b::core::encode_chain(&input, chunk).expect("encode chain");
            let mut session = ChainSession::new();
            for (i, b) in blocks.iter().enumerate() {
                session.decode_raw(&b.raw).expect("self-check");
                let path = PathBuf::from(&args[1]).join(format!("block-{:08}.lzb", i as u32));
                write(path.to_str().unwrap(), &b.raw);
            }
            println!("wrote {} chain blocks into {}", blocks.len(), args[1]);
        }
        "unchain" => {
            assert_eq!(args.len(), 2, "unchain <dir> <out>");
            let mut paths: Vec<PathBuf> = std::fs::read_dir(&args[0])
                .expect("read dir")
                .filter_map(|e| e.ok().map(|e| e.path()))
                .filter(|p| p.extension().is_some_and(|x| x == "lzb"))
                .collect();
            paths.sort();
            let mut session = ChainSession::new();
            let mut out = Vec::new();
            for p in paths {
                let raw = std::fs::read(&p).expect("read block");
                out.extend_from_slice(&session.decode_raw(&raw).expect("chain decode"));
            }
            write(&args[1], &out);
            println!("decoded chain ({} bytes) -> {}", out.len(), args[1]);
        }
        "hexdump" => {
            assert_eq!(args.len(), 1, "hexdump <in.lzb>");
            let raw = read(&args[0]);
            let h = BlockHeader::decode(&raw).unwrap_or_else(|e| panic!("header: {e}"));
            println!(
                "frame={} index={} prev_digest={:#018x}\ncrc={:#010x} decompressed={}\nheader={} payload={}",
                h.frame_type as u8,
                h.index,
                h.prev_digest,
                h.payload_crc,
                h.decompressed_len,
                HEADER_LEN,
                raw.len() - HEADER_LEN
            );
        }
        other => panic!("unknown command {other}"),
    }
}

fn usage() -> ! {
    eprintln!(
        "usage:\n  lz77b compress <in> <out.lzb>\n  lz77b decode <in.lzb> <out> [dict]\n  \
         lz77b verify <in.lzb> [dict]\n  lz77b chain <in> <dir> <chunk-bytes>\n  \
         lz77b unchain <dir> <out>\n  lz77b hexdump <in.lzb>"
    );
    std::process::exit(2);
}
