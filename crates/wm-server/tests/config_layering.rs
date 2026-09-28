//! Configuration layering tests: file < env < CLI.

use std::path::PathBuf;

use wm_server::Config;

fn tmp_config(body: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("wm-cfg-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("config.toml");
    std::fs::write(&path, body).unwrap();
    path
}

#[test]
fn defaults_when_nothing_provided() {
    let cfg = Config::load(None, None, None, None, None).unwrap();
    assert_eq!(cfg.host, "127.0.0.1");
    assert_eq!(cfg.port, 8080);
    assert_eq!(cfg.log_level, "info");
    assert_eq!(cfg.data_dir, PathBuf::from("./data"));
    assert!(cfg.source.is_none());
}

#[test]
fn file_values_load_and_cli_overrides() {
    let path = tmp_config(
        "[data]\ndir = '/var/lib/wm'\n[server]\nhost = '0.0.0.0'\nport = 9000\n[log]\nlevel = 'debug'\n",
    );
    let cfg = Config::load(Some(&path), None, None, None, None).unwrap();
    assert_eq!(cfg.data_dir, PathBuf::from("/var/lib/wm"));
    assert_eq!(cfg.host, "0.0.0.0");
    assert_eq!(cfg.port, 9000);
    assert_eq!(cfg.log_level, "debug");
    assert_eq!(cfg.source, Some(path.clone()));

    // CLI beats file.
    let cfg = Config::load(
        Some(&path),
        Some("127.0.0.1".to_string()),
        Some(7000),
        Some(PathBuf::from("/tmp/wm")),
        Some("error".to_string()),
    )
    .unwrap();
    assert_eq!(cfg.host, "127.0.0.1");
    assert_eq!(cfg.port, 7000);
    assert_eq!(cfg.data_dir, PathBuf::from("/tmp/wm"));
    assert_eq!(cfg.log_level, "error");
}

#[test]
fn malformed_config_is_rejected_with_error() {
    let dir = std::env::temp_dir().join(format!("wm-cfg-bad-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("bad.toml");
    std::fs::write(&path, "this is = = not toml [").unwrap();
    let err = Config::load(Some(&path), None, None, None, None).unwrap_err();
    assert!(err.to_string().contains("parse"), "got: {err}");
}
