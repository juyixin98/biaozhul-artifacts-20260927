//! # rbtool
//!
//! 夹具与数据文件的命令行工具（无服务端依赖）：
//!
//! ```text
//! rbtool gen-fixtures <out_dir>
//!     把全部标准夹具写成 <fixture>.json（整数数组）和 <fixture>.rbs（二进制），
//!     并生成 manifest.json 记录每个夹具的基数/容器种类与说明。
//!
//! rbtool inspect <file.rbs> [--limit N]
//!     严格校验并打印二进制文件结构（魔数/版本/容器逐项：键、类型、基数、
//!     负载长度），损坏时以非零码退出并打印具体错误类别。
//!
//! rbtool import-json <name> <data_dir> <file.json>
//!     把 JSON 整数数组夹具导入为 <data_dir>/<name>.rbs。
//! ```
//!
//! 该工具不展开集合做运算，只做结构检查与格式转换。

#![forbid(unsafe_code)]

use std::path::PathBuf;
use std::process::ExitCode;

use rb_format::{codec, Container, RoaringSet, ARRAY_MAX_CARDINALITY, FORMAT_VERSION, MAGIC};
use rb_persist::fixtures;
use rb_testkit::all_fixtures;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("rbtool: {e}");
            ExitCode::from(2)
        }
    }
}

fn run(args: &[String]) -> Result<(), String> {
    let cmd = args.get(1).map(|s| s.as_str()).unwrap_or("help");
    match cmd {
        "gen-fixtures" => {
            let out = PathBuf::from(args.get(2).ok_or("usage: gen-fixtures <out_dir>")?);
            gen_fixtures(&out)
        }
        "inspect" => {
            let file = PathBuf::from(args.get(2).ok_or("usage: inspect <file.rbs>")?);
            inspect(&file)
        }
        "import-json" => {
            let name = args
                .get(2)
                .ok_or("usage: import-json <name> <data_dir> <file.json>")?;
            let data_dir = PathBuf::from(args.get(3).ok_or("missing data_dir")?);
            let json_path = PathBuf::from(args.get(4).ok_or("missing json file")?);
            import_json(name, &data_dir, &json_path)
        }
        "--help" | "-h" | "help" => {
            print_help();
            Ok(())
        }
        other => Err(format!("unknown command {other:?}; try `rbtool help`")),
    }
}

fn print_help() {
    println!(
        "rbtool {VERSION}\n\
         \n\
         USAGE:\n\
         \trbtool gen-fixtures <out_dir>\n\
         \trbtool inspect <file.rbs>\n\
         \trbtool import-json <name> <data_dir> <file.json>\n\
         \n\
         FORMAT: magic={:?} version={:#010x} array/bitmap threshold={}",
        std::str::from_utf8(&MAGIC).unwrap_or("????"),
        FORMAT_VERSION,
        ARRAY_MAX_CARDINALITY
    );
}

const VERSION: &str = "1.0.0";

// ---------------- gen-fixtures ----------------

fn gen_fixtures(out: &PathBuf) -> Result<(), String> {
    std::fs::create_dir_all(out).map_err(|e| format!("create {out:?}: {e}"))?;
    let mut manifest = Vec::new();
    for fx in all_fixtures() {
        let set = RoaringSet::from_values(fx.values.iter().copied());

        let json = fixtures::to_json(&set);
        let json_path = out.join(format!("{}.json", fx.name));
        std::fs::write(&json_path, &json).map_err(|e| format!("write {json_path:?}: {e}"))?;

        let bin = codec::encode(&set);
        let rbs_path = out.join(format!("{}.rbs", fx.name));
        std::fs::write(&rbs_path, &bin).map_err(|e| format!("write {rbs_path:?}: {e}"))?;

        let (arrays, bitmaps) = kinds(&set);
        manifest.push(serde_json::json!({
            "name": fx.name,
            "description": fx.description,
            "cardinality": set.len_u64(),
            "containers": set.container_count(),
            "array_containers": arrays,
            "bitmap_containers": bitmaps,
            "min": set.min(),
            "max": set.max(),
            "json_file": format!("{}.json", fx.name),
            "rbs_file": format!("{}.rbs", fx.name),
            "rbs_bytes": bin.len(),
        }));
        println!(
            "wrote {name}: card={card} containers={n} (array={arrays}, bitmap={bitmaps})",
            name = fx.name,
            card = set.len_u64(),
            n = set.container_count()
        );
    }
    let manifest_path = out.join("manifest.json");
    let doc = serde_json::json!({
        "format_version": format!("{:#010x}", FORMAT_VERSION),
        "threshold": ARRAY_MAX_CARDINALITY,
        "container_bits": rb_format::CONTAINER_BITS,
        "max_value": rb_format::MAX_VALUE,
        "fixtures": manifest,
    });
    std::fs::write(
        &manifest_path,
        serde_json::to_vec_pretty(&doc).map_err(|e| e.to_string())?,
    )
    .map_err(|e| format!("write {manifest_path:?}: {e}"))?;
    println!("wrote manifest at {manifest_path:?}");
    Ok(())
}

fn kinds(set: &RoaringSet) -> (usize, usize) {
    let mut a = 0;
    let mut b = 0;
    for i in 0..set.container_count() {
        match set.container_at(i) {
            Container::Array(_) => a += 1,
            Container::Bitmap(_) => b += 1,
        }
    }
    (a, b)
}

// ---------------- inspect ----------------

fn inspect(path: &PathBuf) -> Result<(), String> {
    let bytes = std::fs::read(path).map_err(|e| format!("read {path:?}: {e}"))?;
    let set = codec::decode(&bytes).map_err(|e| format!("REJECTED ({})", classify(&e)))?;

    println!("file: {path:?}");
    println!("bytes: {}", bytes.len());
    println!(
        "magic: {:?}",
        std::str::from_utf8(&bytes[0..4]).unwrap_or("????")
    );
    println!(
        "version: {:#010x}",
        u32::from_le_bytes(bytes[4..8].try_into().unwrap())
    );
    println!("cardinality: {}", set.len_u64());
    println!("containers: {}", set.container_count());
    for i in 0..set.container_count() {
        let key = set.keys()[i];
        let (kind, expected_payload) = match set.container_at(i) {
            Container::Array(v) => ("array", v.len() * 2),
            Container::Bitmap(_) => ("bitmap", rb_format::BITMAP_WORDS * 8),
        };
        println!(
            "  [{i:>3}] high_key={key:>5} (values {:#08x}..{:#08x}) kind={kind:<6} card={:<6} payload_bytes={expected_payload}",
            (key as u32) << 16,
            ((key as u32) << 16) | 0xFFFF,
            set.container_at(i).len()
        );
    }
    Ok(())
}

/// 错误类别短名（与 HTTP API 的错误代码同源）。
fn classify(e: &rb_format::CodecError) -> &'static str {
    use rb_format::CodecError::*;
    match e {
        Truncated { .. } => "corrupt_truncated",
        BadMagic(_) => "corrupt_bad_magic",
        UnsupportedVersion(_) => "corrupt_unsupported_version",
        HeaderChecksumMismatch { .. } => "corrupt_header_checksum",
        BodyChecksumMismatch { .. } => "corrupt_body_checksum",
        UnknownContainerTag(_) => "corrupt_unknown_container_tag",
        KeysNotSortedUnique { .. } => "corrupt_keys_not_sorted",
        BadOffset { .. } => "corrupt_bad_offset",
        BadPayloadLength { .. } => "corrupt_bad_payload_length",
        ArrayNotSortedUnique { .. } => "corrupt_array_not_sorted",
        ArrayExceedsThreshold { .. } => "corrupt_threshold_violation",
        CardinalityMismatch { .. } => "corrupt_cardinality_mismatch",
        TrailingBitsSet { .. } => "corrupt_trailing_bits",
        Io(_) => "io_error",
    }
}

// ---------------- import-json ----------------

fn import_json(name: &str, data_dir: &PathBuf, json_path: &PathBuf) -> Result<(), String> {
    if !rb_persist::naming::is_valid_name(name) {
        return Err(format!("invalid name {name:?}"));
    }
    let bytes = std::fs::read(json_path).map_err(|e| format!("read {json_path:?}: {e}"))?;
    let set = fixtures::parse_json(&bytes).map_err(|e| e.to_string())?;
    let store = rb_persist::Store::open(data_dir).map_err(|e| e.to_string())?;
    store.save(name, &set).map_err(|e| e.to_string())?;
    println!(
        "imported {name}: {} elements into {}",
        set.len_u64(),
        data_dir.join(format!("{name}.rbs")).display()
    );
    Ok(())
}
