//! MCP server 2: `shell` (SPEC.md section 3).

use std::io::Read;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::mpsc;
use std::time::{Duration, Instant};

use rmcp::model::{CallToolRequestParams, CallToolResponse, ListToolsResult, PaginatedRequestParams, Tool};
use rmcp::service::RequestContext;
use rmcp::{ErrorData, RoleServer, ServerHandler};
use serde::Serialize;
use serde_json::{Map, Value, json};

use crate::mcp_util::{self, ToolOut};

const TIMEOUT: Duration = Duration::from_secs(30);
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

struct RunOut {
    exit_code: i32,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
    duration_ms: u64,
}

fn reader<R: Read + Send + 'static>(mut r: R) -> std::thread::JoinHandle<Vec<u8>> {
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = r.read_to_end(&mut buf);
        buf
    })
}

/// Run argv in `cwd` with no shell, a minimal environment and a 30 s timeout.
/// With `combined`, stdout and stderr share one pipe (interleaved as written).
fn run(argv: &[String], cwd: &Path, combined: bool) -> Result<RunOut, String> {
    let t0 = Instant::now();
    let mut cmd = Command::new(&argv[0]);
    cmd.args(&argv[1..]).current_dir(cwd).env_clear().stdin(Stdio::null()).process_group(0);
    for k in ["PATH", "HOME", "LANG"] {
        if let Some(v) = std::env::var_os(k) {
            cmd.env(k, v);
        }
    }
    let (out_h, err_h);
    let mut child;
    if combined {
        let (r, w) = std::io::pipe().map_err(|e| format!("pipe: {e}"))?;
        let w2 = w.try_clone().map_err(|e| format!("pipe: {e}"))?;
        cmd.stdout(w).stderr(w2);
        child = cmd.spawn().map_err(|e| format!("cannot run {}: {e}", argv[0]))?;
        drop(cmd); // closes our copies of the write end
        out_h = reader(r);
        err_h = None;
    } else {
        cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
        child = cmd.spawn().map_err(|e| format!("cannot run {}: {e}", argv[0]))?;
        drop(cmd);
        out_h = reader(child.stdout.take().unwrap());
        err_h = Some(reader(child.stderr.take().unwrap()));
    }
    let pid = child.id() as i32;
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(child.wait());
    });
    let status = match rx.recv_timeout(TIMEOUT) {
        Ok(s) => Some(s),
        Err(_) => None,
    };
    // Kill the whole process group: on timeout, and afterwards to reap stragglers that would
    // otherwise keep the output pipes open.
    unsafe {
        kill(-pid, SIGKILL);
    }
    let exit_code = match status {
        None => {
            let _ = rx.recv();
            -1
        }
        Some(Ok(st)) => st.code().unwrap_or_else(|| st.signal().map(|s| 128 + s).unwrap_or(-1)),
        Some(Err(e)) => return Err(format!("wait failed: {e}")),
    };
    let stdout = out_h.join().unwrap_or_default();
    let stderr = err_h.map(|h| h.join().unwrap_or_default()).unwrap_or_default();
    Ok(RunOut { exit_code, stdout, stderr, duration_ms: t0.elapsed().as_millis() as u64 })
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
                let argv = self.test_command.clone();
                let root = self.root.clone();
                let r = tokio::task::spawn_blocking(move || run(&argv, &root, true))
                    .await
                    .map_err(|e| e.to_string())??;
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
                let root = self.root.clone();
                let r = tokio::task::spawn_blocking(move || run(&argv, &root, false))
                    .await
                    .map_err(|e| e.to_string())??;
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
