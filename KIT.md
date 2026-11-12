# Offline kits (/Users/Shared/tc-kit, built 11 Nov 2026, world-readable, never written by a session)

| Lane | What | Versions | How a session uses it |
|---|---|---|---|
| PY | `pyenv/` venv on uv CPython 3.12.13 (`py/`) | mcp 2.3.0, anthropic 2.8.0 | `PATH` starts with `/Users/Shared/tc-kit/pyenv/bin`; packages import directly |
| TS | `npm-cache/` (71 MB), seeded from `ts-seed/` | @modelcontextprotocol/sdk 1.32.2, @anthropic-ai/sdk 1.8.0, zod 4.6.7, commander 15.1.0, typescript 7.0.2, @types/node 26.6.6; Node 26.13.0 (Homebrew) | The recorder copies the cache into the workspace (npm must write to it) and sets `npm_config_cache` + `npm_config_offline=true` |
| RS | `rs-vendor/` (cargo vendor, 236 crates), `rs-vendor-config.toml` | rmcp 3.9.0, tokio 1.54.1, serde 1.0.229, serde_json 1.0.154, schemars 1.2.3, regex 1.13.1, reqwest (json, rustls, http2), anyhow 1.0.104, clap 4.6.7; cargo/rustc 1.99.1 (Homebrew) | The bundle holds `.cargo/config.toml` (source replacement); `CARGO_HOME` points inside the workspace; build with `--offline` |

Checked 11 Nov as `agentlab` (outside the sandbox profile): Python imports, an offline npm install of all six
packages from a copied cache, and an offline `cargo build --release` against the vendor dir all work.
Also checked inside `sandbox-exec` (see HISTORY.md).
