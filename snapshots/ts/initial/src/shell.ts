// MCP server 2: `shell` (SPEC section 3).
import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { commandRejection } from "./allow.js";
import { errorResult, lastChars, okResult, ToolFail, type ToolResult } from "./result.js";

const TIMEOUT_MS = 30_000;
const KEEP_CHARS = 8_000;
// Bytes kept per stream while a command runs; far more than KEEP_CHARS characters can need.
const KEEP_BYTES = 256 * 1024;

const TOOLS = [
  {
    name: "run_tests",
    description:
      "Run the task's test command in the workspace. Returns exit_code, passed (exit_code == 0), the last 8000 " +
      "characters of combined stdout and stderr, and duration_ms. 30 s timeout.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "run_command",
    description:
      "Run one allowlisted command in the workspace without a shell: ls, cat, head, tail, wc, diff, python3, or " +
      "git status|diff|log|show. Returns exit_code, stdout, stderr (last 8000 characters each) and duration_ms. " +
      "30 s timeout.",
    inputSchema: {
      type: "object",
      properties: {
        argv: {
          type: "array",
          items: { type: "string" },
          minItems: 1,
          description: "Program and arguments, e.g. [\"git\", \"diff\"].",
        },
      },
      required: ["argv"],
    },
  },
];

let WS = "";
let TASK_FILE = "";

/** Keeps the tail of a byte stream within a bounded buffer. */
class Tail {
  chunks: Buffer[] = [];
  size = 0;
  push(b: Buffer): void {
    this.chunks.push(b);
    this.size += b.length;
    while (this.chunks.length > 1 && this.size - this.chunks[0].length >= KEEP_BYTES) {
      this.size -= this.chunks.shift()!.length;
    }
  }
  text(): string {
    return lastChars(Buffer.concat(this.chunks).toString("utf8"), KEEP_CHARS);
  }
}

interface ProcResult {
  exitCode: number;
  stdout: Tail;
  stderr: Tail;
  combined: Tail;
  ms: number;
}

function childEnv(): Record<string, string> {
  const env: Record<string, string> = {};
  for (const k of ["PATH", "HOME", "LANG"]) {
    const v = process.env[k];
    if (v !== undefined) env[k] = v;
  }
  return env;
}

function runProcess(argv: string[]): Promise<ProcResult> {
  return new Promise((resolve, reject) => {
    const t0 = performance.now();
    const stdout = new Tail();
    const stderr = new Tail();
    const combined = new Tail();
    let timedOut = false;
    let settled = false;
    const child = spawn(argv[0], argv.slice(1), {
      cwd: WS,
      env: childEnv(),
      stdio: ["ignore", "pipe", "pipe"],
      detached: true, // own process group, so a timeout kills grandchildren too
      shell: false,
    });
    const killGroup = () => {
      try {
        if (child.pid !== undefined) process.kill(-child.pid, "SIGKILL");
      } catch {
        try {
          child.kill("SIGKILL");
        } catch {
          /* already gone */
        }
      }
    };
    const finish = (code: number | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (grace) clearTimeout(grace);
      resolve({
        exitCode: timedOut || code === null ? -1 : code,
        stdout,
        stderr,
        combined,
        ms: Math.round(performance.now() - t0),
      });
    };
    let grace: NodeJS.Timeout | undefined;
    const timer = setTimeout(() => {
      timedOut = true;
      killGroup();
      // If something still holds the pipes open, stop waiting shortly after the kill.
      grace = setTimeout(() => {
        child.stdout?.destroy();
        child.stderr?.destroy();
        finish(null);
      }, 2000);
    }, TIMEOUT_MS);
    child.stdout!.on("data", (b: Buffer) => {
      stdout.push(b);
      combined.push(b);
    });
    child.stderr!.on("data", (b: Buffer) => {
      stderr.push(b);
      combined.push(b);
    });
    child.on("error", (e: NodeJS.ErrnoException) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (grace) clearTimeout(grace);
      reject(new ToolFail(e.code === "ENOENT" ? `command not found: ${argv[0]}` : `cannot run ${argv[0]}: ${e.message}`));
    });
    child.on("close", (code) => finish(code));
  });
}

function readTestCommand(): string[] {
  let raw: string;
  try {
    raw = fs.readFileSync(TASK_FILE, "utf8");
  } catch {
    throw new ToolFail(`cannot read task file: ${TASK_FILE}`);
  }
  let cmd: unknown;
  try {
    cmd = (JSON.parse(raw) as { test_command?: unknown }).test_command;
  } catch {
    throw new ToolFail("task file is not valid JSON");
  }
  if (!Array.isArray(cmd) || cmd.length < 1 || !cmd.every((a) => typeof a === "string")) {
    throw new ToolFail("task file test_command must be a non-empty array of strings");
  }
  return cmd as string[];
}

async function runTests(): Promise<unknown> {
  const r = await runProcess(readTestCommand());
  return { exit_code: r.exitCode, passed: r.exitCode === 0, output: r.combined.text(), duration_ms: r.ms };
}

async function runCommand(args: Record<string, unknown>): Promise<unknown> {
  const reason = commandRejection(args.argv);
  if (reason) throw new ToolFail(reason);
  const r = await runProcess(args.argv as string[]);
  return { exit_code: r.exitCode, stdout: r.stdout.text(), stderr: r.stderr.text(), duration_ms: r.ms };
}

async function call(name: string, args: Record<string, unknown>): Promise<ToolResult> {
  try {
    if (name === "run_tests") return okResult(await runTests());
    if (name === "run_command") return okResult(await runCommand(args));
    return errorResult(`unknown tool: ${name}`);
  } catch (e) {
    return errorResult(e instanceof ToolFail ? e.message : ((e as Error)?.message ?? String(e)));
  }
}

export async function serveShell(workspace: string, task: string): Promise<void> {
  WS = fs.realpathSync(path.resolve(workspace));
  TASK_FILE = path.resolve(task);
  const server = new Server({ name: "tinycoder-shell", version: "1.0.0" }, { capabilities: { tools: {} } });
  server.setRequestHandler(ListToolsRequestSchema, () => ({ tools: TOOLS }));
  server.setRequestHandler(CallToolRequestSchema, (req) =>
    call(req.params.name, (req.params.arguments ?? {}) as Record<string, unknown>),
  );
  await server.connect(new StdioServerTransport());
}
