//! Reads the rewrite-bridge package that Cargo.lock pins and hands its version and source to the program,
//! so the result line names the library it was really built with (never a version typed by hand).

use std::fs;
use std::path::Path;

fn main() {
    let lock = Path::new(&std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("Cargo.lock");
    println!("cargo:rerun-if-changed={}", lock.display());
    let text = fs::read_to_string(&lock).unwrap_or_default();
    let (mut version, mut source) = (String::from("unknown"), String::from("unknown"));
    for block in text.split("[[package]]") {
        let field = |key: &str| {
            block
                .lines()
                .find_map(|l| l.strip_prefix(key).and_then(|r| r.trim().strip_prefix('=')))
                .map(|v| v.trim().trim_matches('"').to_string())
        };
        if field("name").as_deref() == Some("rewrite-bridge") {
            version = field("version").unwrap_or(version);
            source = field("source").unwrap_or(source);
        }
    }
    println!("cargo:rustc-env=LIVE_PEER_REWRITE_BRIDGE_VERSION={version}");
    println!("cargo:rustc-env=LIVE_PEER_REWRITE_BRIDGE_SOURCE={source}");
}
