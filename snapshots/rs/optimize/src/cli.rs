//! Command line and server entry points, shared by both binaries (see BUILD.md / NOTES.md):
//! `tinycoder` (servers; `run` execs `tinycoder-agent`) and `tinycoder-agent` (everything).

use std::path::PathBuf;
use std::pin::Pin;
use std::task::{Context, Poll};

use clap::{Parser, Subcommand};
use rmcp::ServiceExt;
use rmcp::transport::stdio;

use crate::{files, shell};

#[derive(Parser)]
#[command(name = "tinycoder", about = "A small coding agent")]
pub struct Cli {
    #[command(subcommand)]
    pub cmd: Cmd,
}

#[derive(Subcommand)]
pub enum Cmd {
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
        /// Process start time (Unix ns) handed over by the `tinycoder` launcher.
        #[arg(long, hide = true)]
        started_ns: Option<u128>,
    },
}

#[derive(Subcommand)]
pub enum ServeKind {
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

pub fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread().enable_all().build().expect("tokio runtime")
}

/// The server's stdio transport (rmcp does all framing and protocol work on top of it).
///
/// When stdin is a pipe (how MCP clients start stdio servers) it is read through Tokio's
/// non-blocking pipe type on the IO reactor, instead of `tokio::io::stdin()`, which hands every
/// read to a blocking-pool thread (rmcp's `stdio_pipes()` avoids that only on Linux). Replies are
/// written with a plain blocking `write(2)` on the runtime thread: no thread hop, and stdout keeps
/// its blocking mode (measured faster than a non-blocking stdout for byte-at-a-time readers; see
/// NOTES.md). The duplicated stdin descriptor shares its file description with fd 0, so fd 0
/// becomes non-blocking too; nothing else in a server reads fd 0 and child processes get their
/// own stdio, so that is harmless.
async fn serve<S: rmcp::ServerHandler>(server: S) -> anyhow::Result<()> {
    use std::os::fd::AsFd;
    let reader = std::io::stdin()
        .as_fd()
        .try_clone_to_owned()
        .ok()
        .and_then(|fd| tokio::net::unix::pipe::Receiver::from_owned_fd(fd).ok());
    let writer = std::io::stdout().as_fd().try_clone_to_owned().ok().map(std::fs::File::from);
    let running = match (reader, writer) {
        (Some(r), Some(w)) => server.serve((r, InlineWriter(w))).await,
        _ => server.serve(stdio()).await,
    }
    .map_err(|e| anyhow::anyhow!("MCP serve: {e}"))?;
    running.waiting().await?;
    Ok(())
}

/// AsyncWrite over a blocking descriptor that writes inline on the calling thread.
struct InlineWriter(std::fs::File);

impl tokio::io::AsyncWrite for InlineWriter {
    fn poll_write(self: Pin<&mut Self>, _cx: &mut Context<'_>, buf: &[u8]) -> Poll<std::io::Result<usize>> {
        use std::io::Write;
        let f = &mut self.get_mut().0;
        loop {
            match f.write(buf) {
                Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
                r => return Poll::Ready(r),
            }
        }
    }
    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<std::io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

/// `tinycoder serve files|shell`: returns the exit code.
pub fn serve_main(kind: ServeKind) -> i32 {
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

