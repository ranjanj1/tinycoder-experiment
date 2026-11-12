// Build step: produce V8 code caches for the three bundles (see bin/tinycoder).
//
// Each command is run once in a scratch directory with TINYCODER_WRITE_CODE_CACHE=<name>. The launcher then writes
// script.createCachedData() when the process exits. Code cached after a real run holds every function compiled
// during that run, not only the top level, so later starts skip most parsing and compiling.
// Nothing here touches the network: the servers are driven through the official MCP client, and `run` gets a real
// task with an unparseable ANTHROPIC_BASE_URL. It starts and lists both servers, builds the first Messages
// request, and then the Anthropic SDK fails to build the URL ("Invalid URL", not retried), so the run ends with
// status "error" before any connection is made.
import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const ROOT = path.resolve(__dirname, "..", "..");
// Started as an executable, as the harness does, so the shebang's V8 flags match (V8 rejects a code cache made
// under different flags).
const LAUNCHER = path.join(ROOT, "bin", "tinycoder");

function env(name: string): Record<string, string> {
  const e: Record<string, string> = {};
  for (const [k, v] of Object.entries(process.env)) if (v !== undefined) e[k] = v;
  e.TINYCODER_WRITE_CODE_CACHE = name;
  return e;
}

async function warmServer(name: string, args: string[], calls: [string, Record<string, unknown>][]): Promise<void> {
  const client = new Client({ name: "tinycoder-warm", version: "1.0.0" });
  await client.connect(
    new StdioClientTransport({ command: LAUNCHER, args: ["serve", name, ...args], env: env(name) }),
  );
  await client.listTools();
  for (const [tool, a] of calls) await client.callTool({ name: tool, arguments: a });
  await client.close();
}

async function main(): Promise<void> {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "tinycoder-warm-"));
  try {
    const ws = path.join(tmp, "ws");
    fs.mkdirSync(path.join(ws, "pkg"), { recursive: true });
    fs.writeFileSync(path.join(ws, "pkg", "a.py"), "def f(x):\n    return x + 1\n");
    fs.writeFileSync(path.join(ws, "README.md"), "warm-up\n");
    const taskJson = path.join(tmp, "task.json");
    fs.writeFileSync(taskJson, JSON.stringify({ test_command: [process.execPath, "-e", "console.log('ok')"] }));

    for (let i = 0; i < 2; i++) {
      // twice: the second run starts from the first cache and records anything compiled later
      await warmServer("files", ["--workspace", ws], [
        ["list_files", {}],
        ["read_file", { path: "pkg/a.py" }],
        ["read_file", { path: "pkg/a.py", start_line: 1, end_line: 1 }],
        ["write_file", { path: "pkg/b.py", content: "y = 2\n" }],
        ["edit_file", { path: "pkg/b.py", old_text: "2", new_text: "3" }],
        ["search", { pattern: "re?turn", path: "." }],
        ["read_file", { path: "../outside" }],
        ["read_file", { path: "missing.txt" }],
      ]);
      await warmServer("shell", ["--workspace", ws, "--task", taskJson], [
        ["run_tests", {}],
        ["run_command", { argv: ["ls", "-la"] }],
        ["run_command", { argv: ["rm", "x"] }],
      ]);
    }
    const taskDir = path.join(tmp, "task");
    fs.mkdirSync(taskDir);
    fs.writeFileSync(path.join(taskDir, "TASK.md"), "Warm-up task.\n");
    fs.copyFileSync(taskJson, path.join(taskDir, "task.json"));
    const runEnv = env("run");
    runEnv.ANTHROPIC_API_KEY = "warm-up";
    runEnv.ANTHROPIC_BASE_URL = "http://[::1"; // unparseable on purpose (see above)
    runEnv.TINYCODER_RUN_ID = "warm";
    for (let i = 0; i < 2; i++) {
      const r = spawnSync(
        LAUNCHER,
        ["run", "--task", taskDir, "--workspace", ws, "--runs", path.join(tmp, "runs")],
        { env: runEnv, stdio: ["ignore", "pipe", "inherit"], timeout: 60_000 },
      );
      const out = r.stdout.toString();
      if (r.status !== 1 || !out.includes('"status":"error"') || !out.includes("Invalid URL")) {
        throw new Error(`unexpected warm-up run result (exit ${r.status}): ${out}`);
      }
    }
    for (const n of ["files", "shell", "run"]) {
      const f = path.join(ROOT, "dist", `tinycoder-${n}.cache`);
      process.stderr.write(`warm: ${path.relative(ROOT, f)} ${fs.statSync(f).size} bytes\n`);
    }
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

main().catch((e) => {
  process.stderr.write(`warm: ${(e as Error).stack ?? e}\n`);
  process.exit(1);
});
