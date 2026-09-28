//! 命名安全校验与文件名处理。

/// 合法集合名：1–64 个 `[A-Za-z0-9_-]`，不含点号（杜绝 `..`、隐藏名、扩展名混淆）。
pub fn is_valid_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    (1..=64).contains(&bytes.len())
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || *b == b'_' || *b == b'-')
}

/// 返回 `<base>/<name>.rbs`，名称非法返回 `None`。
pub fn data_path(base: &std::path::Path, name: &str) -> Option<std::path::PathBuf> {
    if !is_valid_name(name) {
        return None;
    }
    Some(base.join(format!("{name}.rbs")))
}
