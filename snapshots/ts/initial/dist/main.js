// tinycoder entry point (SPEC section 1).
// Argument parsing uses node:util parseArgs (cheaper to load than commander; server startup time is measured).
import { parseArgs } from "node:util";
const USAGE = `usage:
  tinycoder serve files --workspace <dir>
  tinycoder serve shell --workspace <dir> --task <task.json>
  tinycoder run --task <task-dir> --workspace <dir> --runs <dir>
`;
function usage(msg) {
    if (msg)
        process.stderr.write(`tinycoder: ${msg}\n`);
    process.stderr.write(USAGE);
    process.exit(1);
}
function options(argv, names) {
    let values;
    try {
        ({ values } = parseArgs({
            args: argv,
            options: Object.fromEntries(names.map((n) => [n, { type: "string" }])),
            strict: true,
            allowPositionals: false,
        }));
    }
    catch (e) {
        usage(e.message);
    }
    for (const n of names)
        if (typeof values[n] !== "string")
            usage(`missing --${n}`);
    return values;
}
async function main() {
    const argv = process.argv.slice(2);
    if (argv[0] === "serve" && argv[1] === "files") {
        const o = options(argv.slice(2), ["workspace"]);
        const { serveFiles } = await import("./files.js");
        await serveFiles(o.workspace);
    }
    else if (argv[0] === "serve" && argv[1] === "shell") {
        const o = options(argv.slice(2), ["workspace", "task"]);
        const { serveShell } = await import("./shell.js");
        await serveShell(o.workspace, o.task);
    }
    else if (argv[0] === "run") {
        const o = options(argv.slice(1), ["task", "workspace", "runs"]);
        const { run } = await import("./agent.js");
        process.exit(await run({ task: o.task, workspace: o.workspace, runs: o.runs }));
    }
    else if (argv[0] === "--help" || argv[0] === "-h" || argv[0] === "help") {
        process.stdout.write(USAGE);
    }
    else {
        usage(argv.length ? `unknown command: ${argv.join(" ")}` : undefined);
    }
}
main().catch((e) => {
    process.stderr.write(`tinycoder: ${e?.stack ?? String(e)}\n`);
    process.exit(1);
});
