//! `tinycoder-agent`: the full program (agent loop plus both servers). Normally reached through
//! `tinycoder run`, which execs it; its MCP servers are the lean `tinycoder` binary next to it.

mod agent;
mod cli;
mod files;
mod mcp_util;
mod shell;

use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clap::Parser;

fn main() {
    let now = Instant::now();
    let code = match cli::Cli::parse().cmd {
        cli::Cmd::Serve { kind } => cli::serve_main(kind),
        cli::Cmd::Run { task, workspace, runs, started_ns } => {
            // Count wall time from the launcher's start when it handed one over.
            let t0 = started_ns
                .and_then(|s| {
                    let unix_now = SystemTime::now().duration_since(UNIX_EPOCH).ok()?.as_nanos();
                    let ago = u64::try_from(unix_now.checked_sub(s)?).ok()?;
                    (ago < 60_000_000_000).then(|| now.checked_sub(Duration::from_nanos(ago))).flatten()
                })
                .unwrap_or(now);
            cli::runtime().block_on(agent::run(t0, &task, &workspace, &runs))
        }
    };
    std::process::exit(code);
}
