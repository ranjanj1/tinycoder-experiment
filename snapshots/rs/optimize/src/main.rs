//! `tinycoder`: the entry point. It runs the two MCP servers itself and hands `run` to the
//! `tinycoder-agent` binary next to it (same process, via exec).
//!
//! The split keeps the HTTP/TLS stack out of the servers: reqwest's rustls verifier links
//! Security.framework and CoreFoundation, which costs about 2 ms of dyld time at every
//! server start. See NOTES.md.

mod cli;
mod files;
mod mcp_util;
mod shell;

use std::os::unix::process::CommandExt;
use std::time::{SystemTime, UNIX_EPOCH};

use clap::Parser;

pub const AGENT_BIN: &str = "tinycoder-agent";

fn exec_agent() -> ! {
    let started = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let exe = std::env::current_exe().unwrap_or_else(|_| "tinycoder".into());
    let exe = std::fs::canonicalize(&exe).unwrap_or(exe);
    let agent = exe.with_file_name(AGENT_BIN);
    let err = std::process::Command::new(&agent)
        .args(std::env::args_os().skip(1))
        .arg(format!("--started-ns={started}"))
        .exec();
    // exec only returns on failure. Still honour the one-JSON-line contract.
    eprintln!("tinycoder: cannot start {}: {err}", agent.display());
    let reason = serde_json::Value::String(format!("cannot start {}: {err}", agent.display()));
    println!(
        "{{\"status\":\"error\",\"reason\":{reason},\"steps\":0,\"tool_calls\":0,\"blocked_calls\":0,\
         \"input_tokens\":0,\"output_tokens\":0,\"wall_ms\":0,\"llm_ms\":0,\"tool_ms\":0,\"final_text\":\"\"}}"
    );
    std::process::exit(1);
}

fn main() {
    if std::env::args_os().nth(1).is_some_and(|a| a == "run") {
        exec_agent();
    }
    let code = match cli::Cli::parse().cmd {
        cli::Cmd::Serve { kind } => cli::serve_main(kind),
        cli::Cmd::Run { .. } => exec_agent(),
    };
    std::process::exit(code);
}
