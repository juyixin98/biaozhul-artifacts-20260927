//! Configuration loading tests: defaults, file layer and env overrides.

mod common;

use prereg2d::config::Config;
use std::path::Path;
use tempfile::TempDir;

#[test]
fn defaults_file_and_env_precedence() {
    let rid = common::caselog::run_id();
    let mut log = common::caselog::CaseLog::new(&rid, "config-precedence");

    let d = TempDir::new().unwrap();
    let cfg_path = d.path().join("config.toml");
    std::fs::write(
        &cfg_path,
        "data_dir = \"./fromfile\"\nbind = \"127.0.0.1:9999\"\n",
    )
    .unwrap();

    // File layer overrides defaults.
    let cfg = Config::load(Some(&cfg_path)).unwrap();
    log.step(format!(
        "file: dir={} bind={}",
        cfg.data_dir.display(),
        cfg.bind
    ));
    assert_eq!(cfg.data_dir, Path::new("./fromfile"));
    assert_eq!(cfg.bind, "127.0.0.1:9999");
    assert_eq!(cfg.max_body_bytes, 16 * 1024 * 1024, "default retained");

    // Env layer overrides file.
    std::env::set_var("PREREG2D_BIND", "0.0.0.0:1234");
    let cfg = Config::load(Some(&cfg_path)).unwrap();
    assert_eq!(cfg.bind, "0.0.0.0:1234");
    assert_eq!(cfg.data_dir, Path::new("./fromfile"), "file value retained");
    std::env::remove_var("PREREG2D_BIND");

    // Bad TOML is an error, not a silent default.
    std::fs::write(&cfg_path, "data_dir = ").unwrap();
    let err = Config::load(Some(&cfg_path)).unwrap_err();
    log.step(format!("bad toml: {err}"));
    assert!(err.contains("bad TOML"));

    // Missing file is reported, not silently ignored.
    let err = Config::load(Some(Path::new("/nonexistent/prereg2d-config.toml"))).unwrap_err();
    assert!(err.contains("cannot read config"));

    log.assert_check(true, "config precedence defaults < file < env");
}
