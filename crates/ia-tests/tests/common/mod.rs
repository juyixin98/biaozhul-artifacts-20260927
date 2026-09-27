//! Shared test helpers: locate the repo root and load a named fixture.
#![allow(dead_code)]

pub fn repo_root() -> std::path::PathBuf {
    // CARGO_MANIFEST_DIR points at crates/ia-tests; repo is two levels up.
    let manifest = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .expect("repo root")
}

pub fn fixture(name: &str) -> String {
    let path = repo_root().join("fixtures").join(name);
    std::fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("read fixture {}: {e}", path.display()))
}

pub fn compile(source: impl AsRef<str>) -> (ia_lang::Program, ia_lang::ProgramInfo) {
    let source = source.as_ref();
    let program = ia_lang::parse(source).expect("fixture must parse");
    let info = ia_lang::resolve(&program).expect("fixture must validate");
    (program, info)
}
