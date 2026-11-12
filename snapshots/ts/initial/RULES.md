# Rules (draft; frozen with bench/freeze.py before the first session)

Goal: build tinycoder exactly as SPEC.md says, in your assigned language, correct first and then efficient.

1. Follow SPEC.md and INTERFACE.md exactly. The harness checks and times everything from outside.
2. Use only your lane's language and allowed libraries (below), installed from the supplied offline kit. No web,
   no downloads, no other versions of tinycoder.
3. Release builds. Compiled languages ship a built binary in `bin/`; the build command goes in `BUILD.md`.
4. Do not change, bypass or inspect the harness, tests or timing. Do not special-case test inputs.
5. Failing a check is a real result. Report it in NOTES.md. Do not hide it.
6. The MCP servers and the MCP client must use the official MCP SDK listed for your lane (SPEC.md section 1).
   Hand-written MCP protocol code makes the version ineligible.

| ID | Language | Allowed |
|---|---|---|
| PY | Python 3.12 | Standard library, `mcp` (official MCP SDK), `anthropic` (official SDK). |
| TS | TypeScript, compiled to JavaScript, run on Node 26 | Node standard library, `@modelcontextprotocol/sdk`, `@anthropic-ai/sdk`, `zod`, `commander`, `typescript`, `@types/node`. |
| RS | Rust 1.99, `cargo build --release` | Standard library, `rmcp` (official MCP SDK), `tokio`, `serde`, `serde_json`, `reqwest` (there is no official Anthropic Rust SDK), `regex`, `schemars`, `anyhow`, `clap`. |
| PY2 | Round 2: Python | Anything in the kit, including the Claude Agent SDK and agent frameworks. Same SPEC outputs. |
| TS2 | Round 2: TypeScript | Anything in the kit, including the Claude Agent SDK. Same SPEC outputs. |
| RS2 | Round 2: Rust | Anything in the kit. Same SPEC outputs. |
