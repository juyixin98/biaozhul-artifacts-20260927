//! Capture the rustc version at build time so evidence run logs record the
//! exact compiler used (read via `option_env!("RUSTC_VERSION")` in tests).
fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    let version =
        std::process::Command::new(std::env::var("RUSTC").unwrap_or_else(|_| "rustc".into()))
            .arg("--version")
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|| "rustc (unknown)".to_string());
    println!("cargo:rustc-env=RUSTC_VERSION={version}");
}
