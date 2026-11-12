//! tinycoder: a small coding agent with two MCP servers (see SPEC.md).

mod agent;
mod files;
mod mcp_util;
mod shell;

use std::path::PathBuf;
use std::time::Instant;

use clap::{Parser, Subcommand};
use rmcp::ServiceExt;
use rmcp::transport::stdio;

#[derive(Parser)]
#[command(name = "tinycoder", about = "A small coding agent")]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run one of the MCP servers on stdio
    Serve {
        #[command(subcommand)]
        kind: ServeKind,
    },
    /// Run the agent on one task
    Run {
        #[arg(long)]
        task: PathBuf,
        #[arg(long)]
        workspace: PathBuf,
        #[arg(long)]
        runs: PathBuf,
    },
}

#[derive(Subcommand)]
enum ServeKind {
    /// File tools
    Files {
        #[arg(long)]
        workspace: PathBuf,
    },
    /// Test and command tools
    Shell {
        #[arg(long)]
        workspace: PathBuf,
        #[arg(long)]
        task: PathBuf,
    },
}

fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread().enable_all().build().expect("tokio runtime")
}

async fn serve<S: rmcp::ServerHandler>(server: S) -> anyhow::Result<()> {
    let running = server.serve(stdio()).await.map_err(|e| anyhow::anyhow!("MCP serve: {e}"))?;
    running.waiting().await?;
    Ok(())
}

fn main() {
    let t0 = Instant::now();
    let cli = Cli::parse();
    let code = match cli.cmd {
        Cmd::Serve { kind } => {
            let res = runtime().block_on(async move {
                match kind {
                    ServeKind::Files { workspace } => serve(files::FilesServer::new(&workspace)?).await,
                    ServeKind::Shell { workspace, task } => serve(shell::ShellServer::new(&workspace, &task)?).await,
                }
            });
            match res {
                Ok(()) => 0,
                Err(e) => {
                    eprintln!("tinycoder: {e:#}");
                    1
                }
            }
        }
        Cmd::Run { task, workspace, runs } => runtime().block_on(agent::run(t0, &task, &workspace, &runs)),
    };
    std::process::exit(code);
}
