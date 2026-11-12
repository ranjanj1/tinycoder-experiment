//! MCP server 2: `shell` (SPEC.md section 3).

use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};

use rmcp::model::{CallToolRequestParams, CallToolResponse, ListToolsResult, PaginatedRequestParams, Tool};
use rmcp::service::RequestContext;
use rmcp::{ErrorData, RoleServer, ServerHandler};
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::mcp_util::{self, ToolOut};

const TIMEOUT: Duration = Duration::from_secs(30);
const READ_GRACE: Duration = Duration::from_secs(2);
const KEEP_CHARS: usize = 8000;
const ALLOWED: &[&str] = &["ls", "cat", "head", "tail", "wc", "diff", "python3", "git"];
const GIT_ALLOWED: &[&str] = &["status", "diff", "log", "show"];

unsafe extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
}
const SIGKILL: i32 = 9;

/// SPEC.md section 3 allowlist. Shared with the agent's pre-call hook.
pub fn command_allowed(argv: &[String]) -> Result<(), String> {
    let first = match argv.first() {
        Some(f) => f.as_str(),
        None => return Err("argv must have at least 1 element".into()),
    };
    if !ALLOWED.contains(&first) {
        return Err(format!("command not allowed: {first}"));
    }
    if first == "git" {
        match argv.get(1) {
            Some(sub) if GIT_ALLOWED.contains(&sub.as_str()) => {}
            Some(sub) => return Err(format!("git subcommand not allowed: {sub}")),
            None => return Err("git needs one of: status, diff, log, show".into()),
        }
    }
    Ok(())
}

/// Parse an `argv` argument: a non-empty array of strings.
pub fn parse_argv(v: Option<&Value>) -> Result<Vec<String>, String> {
    let arr = match v {
        Some(Value::Array(a)) => a,
        Some(_) => return Err("argv must be an array of strings".into()),
        None => return Err("missing required argument: argv".into()),
    };
    let mut out = Vec::with_capacity(arr.len());
    for x in arr {
        match x {
            Value::String(s) => out.push(s.clone()),
            _ => return Err("argv must be an array of strings".into()),
        }
    }
    if out.is_empty() {
        return Err("argv must have at least 1 element".into());
    }
    Ok(out)
}

pub struct ShellServer {
    root: PathBuf,
    test_command: Vec<String>,
    tools: Arc<Vec<Tool>>,
}

impl ShellServer {
    pub fn new(workspace: &Path, task: &Path) -> anyhow::Result<Self> {
        let root = std::fs::canonicalize(workspace)
            .map_err(|e| anyhow::anyhow!("workspace {}: {}", workspace.display(), e))?;
        let raw = std::fs::read_to_string(task).map_err(|e| anyhow::anyhow!("task {}: {}", task.display(), e))?;
        let v: Value = serde_json::from_str(&raw)?;
        let test_command = parse_argv(v.get("test_command")).map_err(|e| anyhow::anyhow!("task.json test_command: {e}"))?;
        Ok(Self { root, test_command, tools: Arc::new(tool_defs()) })
    }
}

/// Look a bare program name up in PATH (the same PATH the child gets), like execvp. Spawning an
/// absolute path lets std use posix_spawn directly; with a changed environment and a bare name it
/// must search PATH itself, which measured about 0.35 ms slower per command on macOS. Returns
/// None (leave it to std) for names containing `/`, an empty PATH entry, or no match.
fn resolve_program(name: &str) -> Option<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    if name.is_empty() || name.contains('/') {
        return None;
    }
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        if dir.as_os_str().is_empty() {
            return None;
        }
        let cand = dir.join(name);
        if let Ok(md) = std::fs::metadata(&cand) {
            if md.is_file() && md.permissions().mode() & 0o111 != 0 {
                return Some(cand);
            }
        }
    }
    None
}

struct RunOut {
    exit_code: i32,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    duration_ms: u64,
}

/// Run argv in `cwd` with no shell, a minimal environment and a 30 s timeout.
/// With `combined`, stdout and stderr share one pipe (interleaved as written).
/// Output pipes are read on the IO reactor and the exit is awaited through Tokio's child reaper,
/// so a command costs no extra threads.
async fn run(argv: &[String], cwd: &Path, combined: bool) -> Result<RunOut, String> {
    use tokio::io::{AsyncRead, AsyncReadExt};
    let t0 = Instant::now();
    let mut cmd = match resolve_program(&argv[0]) {
        Some(abs) => {
            let mut c = Command::new(abs);
            c.arg0(&argv[0]);
            c
        }
        None => Command::new(&argv[0]),
    };
    cmd.args(&argv[1..]).current_dir(cwd).env_clear().stdin(Stdio::null()).process_group(0);
    for k in ["PATH", "HOME", "LANG"] {
        if let Some(v) = std::env::var_os(k) {
            cmd.env(k, v);
        }
    }
    let mut shared = None;
    if combined {
        let (r, w) = std::io::pipe().map_err(|e| format!("pipe: {e}"))?;
        let w2 = w.try_clone().map_err(|e| format!("pipe: {e}"))?;
        cmd.stdout(w).stderr(w2);
        shared = Some(r);
    } else {
        cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    }
    let mut cmd = tokio::process::Command::from(cmd);
    let mut child = cmd.spawn().map_err(|e| format!("cannot run {}: {e}", argv[0]))?;
    drop(cmd); // closes our copies of the shared pipe's write end
    let pid = child.id().map(|p| p as i32).unwrap_or(0);

    let (out, err): (Option<Pin<Box<dyn AsyncRead + Send>>>, Option<Pin<Box<dyn AsyncRead + Send>>>) = match shared {
        Some(r) => {
            let r = tokio::net::unix::pipe::Receiver::from_owned_fd(std::os::fd::OwnedFd::from(r))
                .map_err(|e| format!("pipe: {e}"))?;
            (Some(Box::pin(r)), None)
        }
        None => (
            child.stdout.take().map(|s| Box::pin(s) as Pin<Box<dyn AsyncRead + Send>>),
            child.stderr.take().map(|s| Box::pin(s) as Pin<Box<dyn AsyncRead + Send>>),
        ),
    };
    let (mut out_buf, mut err_buf) = (Vec::new(), Vec::new());
    let status = {
        let reads = async {
            let read_out = async {
                if let Some(mut r) = out {
                    let _ = r.read_to_end(&mut out_buf).await;
                }
            };
            let read_err = async {
                if let Some(mut r) = err {
                    let _ = r.read_to_end(&mut err_buf).await;
                }
            };
            tokio::join!(read_out, read_err);
        };
        let waiter = async {
            let status = tokio::time::timeout(TIMEOUT, child.wait()).await;
            // Kill the whole process group: on timeout, and after a normal exit to stop
            // stragglers that would otherwise keep the output pipes open.
            if pid > 0 {
                unsafe {
                    kill(-pid, SIGKILL);
                }
            }
            match status {
                Ok(r) => r.map(Some),
                Err(_) => child.wait().await.map(|_| None),
            }
        };
        tokio::pin!(reads, waiter);
        tokio::select! {
            _ = &mut reads => waiter.await,
            st = &mut waiter => {
                // A descendant that left the process group could hold a pipe open forever:
                // keep reading only briefly after the exit, and keep what was read.
                let _ = tokio::time::timeout(READ_GRACE, &mut reads).await;
                st
            }
        }
    };
    let exit_code = match status {
        Ok(None) => -1,
        Ok(Some(st)) => st.code().unwrap_or_else(|| st.signal().map(|s| 128 + s).unwrap_or(-1)),
        Err(e) => return Err(format!("wait failed: {e}")),
    };
    Ok(RunOut { exit_code, stdout: out_buf, stderr: err_buf, duration_ms: t0.elapsed().as_millis() as u64 })
}

/// Lossy UTF-8 decode, keeping the last `KEEP_CHARS` characters.
fn tail(bytes: &[u8]) -> String {
    let s = String::from_utf8_lossy(bytes);
    let n = s.chars().count();
    if n <= KEEP_CHARS {
        return s.into_owned();
    }
    let skip = s.char_indices().nth(n - KEEP_CHARS).map(|(i, _)| i).unwrap_or(0);
    s[skip..].to_string()
}

impl ShellServer {
    async fn call(&self, name: &str, args: &Map<String, Value>) -> ToolOut {
        match name {
            "run_tests" => {
                let r = run(&self.test_command, &self.root, true).await?;
                mcp_util::ser(&TestsOut {
                    exit_code: r.exit_code,
                    passed: r.exit_code == 0,
                    output: tail(&r.stdout),
                    duration_ms: r.duration_ms,
                })
            }
            "run_command" => {
                let argv = parse_argv(args.get("argv"))?;
                command_allowed(&argv)?;
                let r = run(&argv, &self.root, false).await?;
                mcp_util::ser(&CommandOut {
                    exit_code: r.exit_code,
                    stdout: tail(&r.stdout),
                    stderr: tail(&r.stderr),
                    duration_ms: r.duration_ms,
                })
            }
            _ => Err(format!("unknown tool: {name}")),
        }
    }
}

#[derive(Serialize)]
struct TestsOut {
    exit_code: i32,
    passed: bool,
    output: String,
    duration_ms: u64,
}

#[derive(Serialize)]
struct CommandOut {
    exit_code: i32,
    stdout: String,
    stderr: String,
    duration_ms: u64,
}

fn tool_defs() -> Vec<Tool> {
    vec![
        mcp_util::tool(
            "run_tests",
            "Run the task's test command in the workspace. Returns exit_code, passed, the last 8000 characters of combined stdout+stderr, and duration_ms.",
            json!({"type": "object", "properties": {}, "required": []}),
        ),
        mcp_util::tool(
            "run_command",
            "Run one command in the workspace without a shell (30 s timeout). Allowed: ls, cat, head, tail, wc, diff, python3, and git status|diff|log|show.",
            json!({"type": "object", "properties": {
                "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                         "description": "Program and arguments, e.g. [\"ls\", \"-la\"]."}
            }, "required": ["argv"]}),
        ),
    ]
}

impl ServerHandler for ShellServer {
    fn get_info(&self) -> rmcp::model::ServerConfig {
        mcp_util::server_info("tinycoder-shell")
    }

    async fn list_tools(
        &self,
        _request: Option<PaginatedRequestParams>,
        _context: RequestContext<RoleServer>,
    ) -> Result<ListToolsResult, ErrorData> {
        Ok(ListToolsResult::with_all_items((*self.tools).clone()))
    }

    async fn call_tool(
        &self,
        request: CallToolRequestParams,
        _context: RequestContext<RoleServer>,
    ) -> Result<CallToolResponse, ErrorData> {
        let empty = Map::new();
        let args = request.arguments.as_ref().unwrap_or(&empty);
        Ok(mcp_util::to_response(self.call(&request.name, args).await))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_program_uses_path_like_execvp() {
        let ls = resolve_program("ls").expect("ls on PATH");
        assert!(ls.is_absolute() && ls.ends_with("ls"));
        assert_eq!(resolve_program("./ls"), None);
        assert_eq!(resolve_program("bin/ls"), None);
        assert_eq!(resolve_program(""), None);
        assert_eq!(resolve_program("no-such-program-tinycoder"), None);
    }

    #[test]
    fn allowlist() {
        let v = |a: &[&str]| a.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert!(command_allowed(&v(&["ls", "-la"])).is_ok());
        assert!(command_allowed(&v(&["git", "log"])).is_ok());
        assert!(command_allowed(&v(&["git", "push"])).is_err());
        assert!(command_allowed(&v(&["git"])).is_err());
        assert!(command_allowed(&v(&["/bin/ls"])).is_err());
        assert!(command_allowed(&v(&["sh", "-c", "ls"])).is_err());
        assert!(command_allowed(&[]).is_err());
    }

    #[test]
    fn tail_keeps_last_chars() {
        let s = "é".repeat(KEEP_CHARS + 5);
        assert_eq!(tail(s.as_bytes()).chars().count(), KEEP_CHARS);
        assert_eq!(tail(b"abc"), "abc");
    }

    #[test]
    fn run_combines_stdout_and_stderr() {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let cwd = std::env::temp_dir();
        let argv: Vec<String> =
            ["python3", "-c", "import sys; print('a', flush=True); print('b', file=sys.stderr, flush=True); sys.exit(3)"]
                .iter()
                .map(|s| s.to_string())
                .collect();
        let r = rt.block_on(run(&argv, &cwd, true)).unwrap();
        assert_eq!(r.exit_code, 3);
        assert_eq!(String::from_utf8_lossy(&r.stdout), "a\nb\n");
    }
}
