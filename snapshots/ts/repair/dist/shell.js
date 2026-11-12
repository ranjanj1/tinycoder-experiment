"use strict";
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.serveShell = serveShell;
// MCP server 2: `shell` (SPEC section 3).
const node_child_process_1 = require("node:child_process");
const fs = __importStar(require("node:fs"));
const path = __importStar(require("node:path"));
const index_js_1 = require("@modelcontextprotocol/sdk/server/index.js");
const stdio_js_1 = require("@modelcontextprotocol/sdk/server/stdio.js");
const types_js_1 = require("@modelcontextprotocol/sdk/types.js");
const allow_js_1 = require("./allow.js");
const validator_js_1 = require("./validator.js");
const result_js_1 = require("./result.js");
const TIMEOUT_MS = 30_000;
const KEEP_CHARS = 8_000;
// Bytes kept per stream while a command runs; far more than KEEP_CHARS characters can need.
const KEEP_BYTES = 256 * 1024;
const TOOLS = [
    {
        name: "run_tests",
        description: "Run the task's test command in the workspace. Returns exit_code, passed (exit_code == 0), the last 8000 " +
            "characters of combined stdout and stderr, and duration_ms. 30 s timeout.",
        inputSchema: { type: "object", properties: {} },
    },
    {
        name: "run_command",
        description: "Run one allowlisted command in the workspace without a shell: ls, cat, head, tail, wc, diff, python3, or " +
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
    chunks = [];
    size = 0;
    push(b) {
        this.chunks.push(b);
        this.size += b.length;
        while (this.chunks.length > 1 && this.size - this.chunks[0].length >= KEEP_BYTES) {
            this.size -= this.chunks.shift().length;
        }
    }
    text() {
        return (0, result_js_1.lastChars)(Buffer.concat(this.chunks).toString("utf8"), KEEP_CHARS);
    }
}
function childEnv() {
    const env = {};
    for (const k of ["PATH", "HOME", "LANG"]) {
        const v = process.env[k];
        if (v !== undefined)
            env[k] = v;
    }
    return env;
}
function runProcess(argv) {
    return new Promise((resolve, reject) => {
        const t0 = performance.now();
        const stdout = new Tail();
        const stderr = new Tail();
        const combined = new Tail();
        let timedOut = false;
        let settled = false;
        const child = (0, node_child_process_1.spawn)(argv[0], argv.slice(1), {
            cwd: WS,
            env: childEnv(),
            stdio: ["ignore", "pipe", "pipe"],
            detached: true, // own process group, so a timeout kills grandchildren too
            shell: false,
        });
        const killGroup = () => {
            try {
                if (child.pid !== undefined)
                    process.kill(-child.pid, "SIGKILL");
            }
            catch {
                try {
                    child.kill("SIGKILL");
                }
                catch {
                    /* already gone */
                }
            }
        };
        const finish = (code) => {
            if (settled)
                return;
            settled = true;
            clearTimeout(timer);
            if (grace)
                clearTimeout(grace);
            resolve({
                exitCode: timedOut || code === null ? -1 : code,
                stdout,
                stderr,
                combined,
                ms: Math.round(performance.now() - t0),
            });
        };
        let grace;
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
        child.stdout.on("data", (b) => {
            stdout.push(b);
            combined.push(b);
        });
        child.stderr.on("data", (b) => {
            stderr.push(b);
            combined.push(b);
        });
        child.on("error", (e) => {
            if (settled)
                return;
            settled = true;
            clearTimeout(timer);
            if (grace)
                clearTimeout(grace);
            reject(new result_js_1.ToolFail(e.code === "ENOENT" ? `command not found: ${argv[0]}` : `cannot run ${argv[0]}: ${e.message}`));
        });
        child.on("close", (code) => finish(code));
    });
}
function readTestCommand() {
    let raw;
    try {
        raw = fs.readFileSync(TASK_FILE, "utf8");
    }
    catch {
        throw new result_js_1.ToolFail(`cannot read task file: ${TASK_FILE}`);
    }
    let cmd;
    try {
        cmd = JSON.parse(raw).test_command;
    }
    catch {
        throw new result_js_1.ToolFail("task file is not valid JSON");
    }
    if (!Array.isArray(cmd) || cmd.length < 1 || !cmd.every((a) => typeof a === "string")) {
        throw new result_js_1.ToolFail("task file test_command must be a non-empty array of strings");
    }
    return cmd;
}
async function runTests() {
    const r = await runProcess(readTestCommand());
    return { exit_code: r.exitCode, passed: r.exitCode === 0, output: r.combined.text(), duration_ms: r.ms };
}
async function runCommand(args) {
    const reason = (0, allow_js_1.commandRejection)(args.argv);
    if (reason)
        throw new result_js_1.ToolFail(reason);
    const r = await runProcess(args.argv);
    return { exit_code: r.exitCode, stdout: r.stdout.text(), stderr: r.stderr.text(), duration_ms: r.ms };
}
async function call(name, args) {
    try {
        if (name === "run_tests")
            return (0, result_js_1.okResult)(await runTests());
        if (name === "run_command")
            return (0, result_js_1.okResult)(await runCommand(args));
        return (0, result_js_1.errorResult)(`unknown tool: ${name}`);
    }
    catch (e) {
        return (0, result_js_1.errorResult)(e instanceof result_js_1.ToolFail ? e.message : (e?.message ?? String(e)));
    }
}
async function serveShell(workspace, task) {
    WS = fs.realpathSync(path.resolve(workspace));
    TASK_FILE = path.resolve(task);
    const server = new index_js_1.Server({ name: "tinycoder-shell", version: "1.0.0" }, { capabilities: { tools: {} }, jsonSchemaValidator: new validator_js_1.LazyValidator() });
    server.setRequestHandler(types_js_1.ListToolsRequestSchema, () => ({ tools: TOOLS }));
    server.setRequestHandler(types_js_1.CallToolRequestSchema, (req) => call(req.params.name, (req.params.arguments ?? {})));
    await server.connect(new stdio_js_1.StdioServerTransport());
}
