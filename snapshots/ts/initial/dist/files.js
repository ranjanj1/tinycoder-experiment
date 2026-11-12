// MCP server 1: `files` (SPEC section 2).
import * as fs from "node:fs";
import * as path from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { errorResult, okResult, ToolFail } from "./result.js";
const MAX_LIST = 500;
const MAX_MATCHES = 200;
const TOOLS = [
    {
        name: "list_files",
        description: "List files and directories recursively under a workspace-relative path (default \".\"), sorted by path. " +
            "Skips .git. At most 500 entries.",
        inputSchema: {
            type: "object",
            properties: {
                path: { type: "string", description: "Workspace-relative directory to list. Default \".\"." },
            },
        },
    },
    {
        name: "read_file",
        description: "Read a UTF-8 text file. Optionally only lines start_line..end_line (1-based, inclusive). " +
            "Returns the content and the file's total line count.",
        inputSchema: {
            type: "object",
            properties: {
                path: { type: "string", description: "Workspace-relative file path." },
                start_line: { type: "integer", minimum: 1, description: "First line to return (1-based)." },
                end_line: { type: "integer", minimum: 1, description: "Last line to return (inclusive, >= start_line)." },
            },
            required: ["path"],
        },
    },
    {
        name: "write_file",
        description: "Write a UTF-8 text file, replacing it if it exists. Creates parent folders.",
        inputSchema: {
            type: "object",
            properties: {
                path: { type: "string", description: "Workspace-relative file path." },
                content: { type: "string", description: "Full new file content." },
            },
            required: ["path", "content"],
        },
    },
    {
        name: "edit_file",
        description: "Replace exactly one occurrence of old_text with new_text in a file. " +
            "Fails if old_text occurs zero times or more than once.",
        inputSchema: {
            type: "object",
            properties: {
                path: { type: "string", description: "Workspace-relative file path." },
                old_text: { type: "string", description: "Exact text to replace; must occur exactly once." },
                new_text: { type: "string", description: "Replacement text." },
            },
            required: ["path", "old_text", "new_text"],
        },
    },
    {
        name: "search",
        description: "Search UTF-8 text files under a workspace-relative path (default \".\") for lines matching a regular " +
            "expression. Results are in path then line order, at most 200.",
        inputSchema: {
            type: "object",
            properties: {
                pattern: { type: "string", description: "Regular expression to match against each line." },
                path: { type: "string", description: "Workspace-relative file or directory. Default \".\"." },
            },
            required: ["pattern"],
        },
    },
];
const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
let WS = "";
function isOutside(rel) {
    return rel === ".." || rel.startsWith(".." + path.sep) || path.isAbsolute(rel);
}
function hasGit(rel) {
    return rel.split(path.sep).includes(".git");
}
function toDisplay(rel) {
    if (rel === "")
        return ".";
    return path.sep === "/" ? rel : rel.split(path.sep).join("/");
}
/** Resolve a workspace-relative path, following symlinks of every existing part. */
function resolvePath(p, label = "path") {
    if (p === undefined || p === null)
        p = ".";
    if (typeof p !== "string")
        throw new ToolFail(`${label} must be a string`);
    if (p === "")
        p = ".";
    const ps = p;
    if (ps.includes("\0"))
        throw new ToolFail(`invalid ${label}`);
    if (path.isAbsolute(ps))
        throw new ToolFail(`absolute paths are not allowed: ${ps}`);
    const lexical = path.resolve(WS, ps);
    const lrel = path.relative(WS, lexical);
    if (isOutside(lrel))
        throw new ToolFail(`path is outside the workspace: ${ps}`);
    if (hasGit(lrel))
        throw new ToolFail(`path inside .git is not allowed: ${ps}`);
    // Follow symlinks: realpath the deepest existing ancestor, then re-append the missing tail.
    let cur = lexical;
    const tail = [];
    let real;
    for (;;) {
        try {
            real = fs.realpathSync(cur);
            break;
        }
        catch (e) {
            const code = e.code;
            if (code !== "ENOENT" && code !== "ENOTDIR")
                throw e;
            const parent = path.dirname(cur);
            if (parent === cur)
                throw new ToolFail(`cannot resolve path: ${ps}`);
            tail.unshift(path.basename(cur));
            cur = parent;
        }
    }
    if (tail.length)
        real = path.join(real, ...tail);
    const rrel = path.relative(WS, real);
    if (isOutside(rrel))
        throw new ToolFail(`path resolves outside the workspace: ${ps}`);
    if (hasGit(rrel))
        throw new ToolFail(`path inside .git is not allowed: ${ps}`);
    return { real, rel: toDisplay(lrel) };
}
function joinRel(base, name) {
    return base === "." ? name : base + "/" + name;
}
/** Whether a symlink target stays inside the workspace and outside .git. */
function linkInside(abs) {
    try {
        const r = path.relative(WS, fs.realpathSync(abs));
        return !isOutside(r) && !hasGit(r);
    }
    catch {
        return false;
    }
}
/** Recursive walk. Does not descend into symlinked directories; skips links leaving the workspace. */
function walk(absDir, relDir, out, filesOnly) {
    let dirents;
    try {
        dirents = fs.readdirSync(absDir, { withFileTypes: true });
    }
    catch {
        return;
    }
    for (const d of dirents) {
        if (d.name === ".git")
            continue;
        const abs = path.join(absDir, d.name);
        const rel = joinRel(relDir, d.name);
        if (d.isDirectory()) {
            if (!filesOnly)
                out.push({ path: rel, type: "dir", size: 0, abs });
            walk(abs, rel, out, filesOnly);
        }
        else if (d.isFile()) {
            let size = 0;
            if (!filesOnly) {
                try {
                    size = fs.statSync(abs).size;
                }
                catch {
                    continue;
                }
            }
            out.push({ path: rel, type: "file", size, abs });
        }
        else if (d.isSymbolicLink()) {
            if (!linkInside(abs))
                continue;
            let st;
            try {
                st = fs.statSync(abs);
            }
            catch {
                continue;
            }
            if (st.isDirectory()) {
                if (!filesOnly)
                    out.push({ path: rel, type: "dir", size: 0, abs });
            }
            else if (st.isFile()) {
                out.push({ path: rel, type: "file", size: st.size, abs });
            }
        }
    }
}
function byPath(a, b) {
    return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
}
function statOrFail(r) {
    try {
        return fs.statSync(r.real);
    }
    catch (e) {
        throw fsFail(e, r.rel);
    }
}
function fsFail(e, rel) {
    if (e instanceof ToolFail)
        return e;
    const code = e.code;
    switch (code) {
        case "ENOENT":
            return new ToolFail(`no such file or directory: ${rel}`);
        case "EISDIR":
            return new ToolFail(`is a directory: ${rel}`);
        case "ENOTDIR":
            return new ToolFail(`not a directory: ${rel}`);
        case "EACCES":
        case "EPERM":
            return new ToolFail(`permission denied: ${rel}`);
        default:
            return new ToolFail(`${code ?? "failed"}: ${rel}`);
    }
}
function readText(r) {
    let buf;
    try {
        buf = fs.readFileSync(r.real);
    }
    catch (e) {
        throw fsFail(e, r.rel);
    }
    try {
        return decoder.decode(buf);
    }
    catch {
        throw new ToolFail(`file is not valid UTF-8: ${r.rel}`);
    }
}
function requireString(args, key) {
    const v = args[key];
    if (typeof v !== "string")
        throw new ToolFail(`${key} is required and must be a string`);
    return v;
}
function optionalLine(args, key) {
    const v = args[key];
    if (v === undefined || v === null)
        return undefined;
    if (typeof v !== "number" || !Number.isInteger(v) || v < 1)
        throw new ToolFail(`${key} must be an integer >= 1`);
    return v;
}
// ---- tools ----
function listFiles(args) {
    const r = resolvePath(args.path);
    const st = statOrFail(r);
    const out = [];
    if (st.isDirectory()) {
        walk(r.real, r.rel, out, false);
    }
    else {
        out.push({ path: r.rel, type: "file", size: st.size, abs: r.real });
    }
    out.sort(byPath);
    const entries = out.slice(0, MAX_LIST).map((e) => ({ path: e.path, type: e.type, size: e.size }));
    if (out.length > MAX_LIST)
        return { entries, truncated: true };
    return { entries };
}
/** Split text into lines that keep their "\n" terminator. */
function splitKeep(text) {
    const lines = [];
    let start = 0;
    for (;;) {
        const i = text.indexOf("\n", start);
        if (i < 0)
            break;
        lines.push(text.slice(start, i + 1));
        start = i + 1;
    }
    if (start < text.length)
        lines.push(text.slice(start));
    return lines;
}
function readFile(args) {
    const r = resolvePath(requireString(args, "path"));
    const start = optionalLine(args, "start_line");
    const end = optionalLine(args, "end_line");
    if (start !== undefined && end !== undefined && end < start)
        throw new ToolFail("end_line must be >= start_line");
    const text = readText(r);
    const lines = splitKeep(text);
    if (start === undefined && end === undefined)
        return { content: text, total_lines: lines.length };
    const s = (start ?? 1) - 1;
    const e = end ?? lines.length;
    return { content: lines.slice(s, e).join(""), total_lines: lines.length };
}
function writeFile(args) {
    const r = resolvePath(requireString(args, "path"));
    const content = requireString(args, "content");
    const buf = Buffer.from(content, "utf8");
    try {
        fs.mkdirSync(path.dirname(r.real), { recursive: true });
        fs.writeFileSync(r.real, buf);
    }
    catch (e) {
        throw fsFail(e, r.rel);
    }
    return { bytes_written: buf.length };
}
function editFile(args) {
    const r = resolvePath(requireString(args, "path"));
    const oldText = requireString(args, "old_text");
    const newText = requireString(args, "new_text");
    if (oldText === "")
        throw new ToolFail("old_text must not be empty");
    const text = readText(r);
    const first = text.indexOf(oldText);
    if (first < 0)
        throw new ToolFail(`old_text not found in ${r.rel}`);
    let count = 1;
    for (let i = text.indexOf(oldText, first + oldText.length); i >= 0; i = text.indexOf(oldText, i + oldText.length)) {
        count++;
    }
    if (count > 1)
        throw new ToolFail(`old_text occurs ${count} times in ${r.rel}; it must occur exactly once`);
    const updated = text.slice(0, first) + newText + text.slice(first + oldText.length);
    try {
        fs.writeFileSync(r.real, updated, "utf8");
    }
    catch (e) {
        throw fsFail(e, r.rel);
    }
    return { replacements: 1 };
}
function compileRegex(pattern) {
    try {
        return new RegExp(pattern, "u");
    }
    catch {
        try {
            return new RegExp(pattern);
        }
        catch (e) {
            throw new ToolFail(`invalid regular expression: ${e.message}`);
        }
    }
}
function search(args) {
    const pattern = requireString(args, "pattern");
    const re = compileRegex(pattern);
    const r = resolvePath(args.path);
    const st = statOrFail(r);
    const files = [];
    if (st.isDirectory()) {
        walk(r.real, r.rel, files, true);
        files.sort(byPath);
    }
    else {
        files.push({ path: r.rel, type: "file", size: st.size, abs: r.real });
    }
    const matches = [];
    for (const f of files) {
        let text;
        try {
            text = decoder.decode(fs.readFileSync(f.abs));
        }
        catch {
            continue; // unreadable or not UTF-8: skipped
        }
        let start = 0;
        let lineNo = 0;
        while (start < text.length) {
            let nl = text.indexOf("\n", start);
            if (nl < 0)
                nl = text.length;
            lineNo++;
            let line = text.slice(start, nl);
            if (line.endsWith("\r"))
                line = line.slice(0, -1);
            start = nl + 1;
            if (re.test(line)) {
                if (matches.length >= MAX_MATCHES)
                    return { matches, truncated: true };
                matches.push({ path: f.path, line: lineNo, text: line });
            }
        }
    }
    return { matches };
}
const HANDLERS = {
    list_files: listFiles,
    read_file: readFile,
    write_file: writeFile,
    edit_file: editFile,
    search,
};
function call(name, args) {
    const h = HANDLERS[name];
    if (!h)
        return errorResult(`unknown tool: ${name}`);
    try {
        return okResult(h(args));
    }
    catch (e) {
        if (e instanceof ToolFail)
            return errorResult(e.message);
        return errorResult(e?.message ?? String(e));
    }
}
export async function serveFiles(workspace) {
    WS = fs.realpathSync(path.resolve(workspace));
    const server = new Server({ name: "tinycoder-files", version: "1.0.0" }, { capabilities: { tools: {} } });
    server.setRequestHandler(ListToolsRequestSchema, () => ({ tools: TOOLS }));
    server.setRequestHandler(CallToolRequestSchema, (req) => call(req.params.name, (req.params.arguments ?? {})));
    await server.connect(new StdioServerTransport());
}
