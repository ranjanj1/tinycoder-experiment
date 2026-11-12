// Drop dynamic libraries that end up unused after dead-stripping (on macOS the `libc` crate
// always declares libiconv): fewer dylibs for dyld to load at every server start.
fn main() {
    if std::env::var("CARGO_CFG_TARGET_VENDOR").as_deref() == Ok("apple") {
        println!("cargo:rustc-link-arg=-Wl,-dead_strip_dylibs");
    }
}
