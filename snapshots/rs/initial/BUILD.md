# Build

From the workspace root (uses the vendored crates configured in `.cargo/config.toml`, no network):

```sh
cargo build --release --offline
mkdir -p bin && cp target/release/tinycoder bin/tinycoder
```

`bin/tinycoder` is the release binary itself. It reads `shared/system.txt` at run time by walking
up from the binary's location (so keep `bin/` and `shared/` side by side).

Optional: `cargo test --release --offline` runs the unit test for the search scanner.
