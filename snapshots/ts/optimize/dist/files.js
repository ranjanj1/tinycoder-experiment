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
exports.serveFiles = serveFiles;
// MCP server 1: `files` (SPEC section 2).
const fs = __importStar(require("node:fs"));
const path = __importStar(require("node:path"));
const index_js_1 = require("@modelcontextprotocol/sdk/server/index.js");
const stdio_js_1 = require("@modelcontextprotocol/sdk/server/stdio.js");
const types_js_1 = require("@modelcontextprotocol/sdk/types.js");
const validator_js_1 = require("./validator.js");
const result_js_1 = require("./result.js");
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
        throw new result_js_1.ToolFail(`${label} must be a string`);
    if (p === "")
        p = ".";
    const ps = p;
    if (ps.includes("\0"))
        throw new result_js_1.ToolFail(`invalid ${label}`);
    if (path.isAbsolute(ps))
        throw new result_js_1.ToolFail(`absolute paths are not allowed: ${ps}`);
    const lexical = path.resolve(WS, ps);
    const lrel = path.relative(WS, lexical);
    if (isOutside(lrel))
        throw new result_js_1.ToolFail(`path is outside the workspace: ${ps}`);
    if (hasGit(lrel))
        throw new result_js_1.ToolFail(`path inside .git is not allowed: ${ps}`);
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
                throw new result_js_1.ToolFail(`cannot resolve path: ${ps}`);
            tail.unshift(path.basename(cur));
            cur = parent;
        }
    }
    if (tail.length)
        real = path.join(real, ...tail);
    const rrel = path.relative(WS, real);
    if (isOutside(rrel))
        throw new result_js_1.ToolFail(`path resolves outside the workspace: ${ps}`);
    if (hasGit(rrel))
        throw new result_js_1.ToolFail(`path inside .git is not allowed: ${ps}`);
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
const dirCache = new Map();
const textCache = new Map();
const CACHE_MAX_FILE = 1 << 20; // bytes; larger files are read every time
const CACHE_BUDGET = 16 << 20; // total cached characters before the text cache is cleared
let cachedChars = 0;
const RACY_NS = 2000000000n;
function settled(st) {
    const now = BigInt(Date.now()) * 1000000n;
    return now - st.mtimeNs >= RACY_NS && now - st.ctimeNs >= RACY_NS;
}
function forget(abs) {
    const c = textCache.get(abs);
    if (c) {
        cachedChars -= c.text?.length ?? 0;
        textCache.delete(abs);
    }
}
function listDir(absDir) {
    let st;
    try {
        st = fs.statSync(absDir, { bigint: true, throwIfNoEntry: false });
    }
    catch {
        st = undefined;
    }
    if (!st) {
        dirCache.delete(absDir);
        return null;
    }
    const c = dirCache.get(absDir);
    if (c && c.ino === st.ino && c.mtime === st.mtimeNs && c.ctime === st.ctimeNs)
        return c.entries;
    let dirents;
    try {
        dirents = fs.readdirSync(absDir, { withFileTypes: true });
    }
    catch {
        dirCache.delete(absDir);
        return null;
    }
    const entries = dirents.map((d) => ({
        name: d.name,
        kind: (d.isDirectory() ? "dir" : d.isFile() ? "file" : d.isSymbolicLink() ? "link" : "other"),
    }));
    if (settled(st))
        dirCache.set(absDir, { ino: st.ino, mtime: st.mtimeNs, ctime: st.ctimeNs, entries });
    else
        dirCache.delete(absDir);
    return entries;
}
/** Recursive walk. Does not descend into symlinked directories; skips links leaving the workspace. */
function walk(absDir, relDir, out, filesOnly) {
    const entries = listDir(absDir);
    if (!entries)
        return;
    for (const d of entries) {
        if (d.name === ".git")
            continue;
        const abs = path.join(absDir, d.name);
        const rel = joinRel(relDir, d.name);
        if (d.kind === "dir") {
            if (!filesOnly)
                out.push({ path: rel, type: "dir", size: 0, abs });
            walk(abs, rel, out, filesOnly);
        }
        else if (d.kind === "file") {
            let size = 0;
            if (!filesOnly) {
                const st = fs.statSync(abs, { throwIfNoEntry: false });
                if (!st)
                    continue;
                size = st.size;
            }
            out.push({ path: rel, type: "file", size, abs });
        }
        else if (d.kind === "link") {
            if (!linkInside(abs))
                continue;
            let st;
            try {
                st = fs.statSync(abs, { throwIfNoEntry: false });
            }
            catch {
                continue;
            }
            if (!st)
                continue;
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
/** Text of each file for search (null: unreadable or not UTF-8). Cache hits need one stat; misses are read in
 * parallel on libuv's thread pool. */
async function searchTexts(files) {
    const texts = new Array(files.length).fill(null);
    const misses = [];
    for (let i = 0; i < files.length; i++) {
        const abs = files[i].abs;
        let st;
        try {
            st = fs.statSync(abs, { bigint: true, throwIfNoEntry: false });
        }
        catch {
            st = undefined;
        }
        if (!st || !st.isFile()) {
            forget(abs);
            continue;
        }
        const c = textCache.get(abs);
        if (c && c.ino === st.ino && c.size === st.size && c.mtime === st.mtimeNs && c.ctime === st.ctimeNs) {
            texts[i] = c.text;
            continue;
        }
        const key = st;
        misses.push(new Promise((resolve) => {
            fs.readFile(abs, (err, buf) => {
                if (err) {
                    forget(abs);
                    return resolve();
                }
                let text;
                try {
                    text = decoder.decode(buf);
                }
                catch {
                    text = null;
                }
                texts[i] = text;
                forget(abs);
                if (buf.length <= CACHE_MAX_FILE && BigInt(buf.length) === key.size && settled(key)) {
                    // stat was taken before the read, so a file changed in between is simply read again next time
                    if (cachedChars + (text?.length ?? 0) > CACHE_BUDGET) {
                        textCache.clear();
                        cachedChars = 0;
                    }
                    textCache.set(abs, { ino: key.ino, size: key.size, mtime: key.mtimeNs, ctime: key.ctimeNs, text });
                    cachedChars += text?.length ?? 0;
                }
                resolve();
            });
        }));
    }
    await Promise.all(misses);
    return texts;
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
    if (e instanceof result_js_1.ToolFail)
        return e;
    const code = e.code;
    switch (code) {
        case "ENOENT":
            return new result_js_1.ToolFail(`no such file or directory: ${rel}`);
        case "EISDIR":
            return new result_js_1.ToolFail(`is a directory: ${rel}`);
        case "ENOTDIR":
            return new result_js_1.ToolFail(`not a directory: ${rel}`);
        case "EACCES":
        case "EPERM":
            return new result_js_1.ToolFail(`permission denied: ${rel}`);
        default:
            return new result_js_1.ToolFail(`${code ?? "failed"}: ${rel}`);
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
        throw new result_js_1.ToolFail(`file is not valid UTF-8: ${r.rel}`);
    }
}
function requireString(args, key) {
    const v = args[key];
    if (typeof v !== "string")
        throw new result_js_1.ToolFail(`${key} is required and must be a string`);
    return v;
}
function optionalLine(args, key) {
    const v = args[key];
    if (v === undefined || v === null)
        return undefined;
    if (typeof v !== "number" || !Number.isInteger(v) || v < 1)
        throw new result_js_1.ToolFail(`${key} must be an integer >= 1`);
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
/** Lines are split on "\n" and keep it; a trailing newline does not start a new line. Returns the slice from line
 * `start` to line `end` (1-based, inclusive, clamped) and the line count, without building a line array. */
function lineSlice(text, start, end) {
    let total = 0;
    let pos = 0;
    let sOff = start === 1 ? 0 : -1;
    let eOff = -1;
    for (;;) {
        const i = text.indexOf("\n", pos);
        if (i < 0)
            break;
        total++;
        pos = i + 1;
        if (total === start - 1)
            sOff = pos;
        if (total === end)
            eOff = pos;
    }
    if (pos < text.length)
        total++;
    if (sOff < 0)
        return { content: "", total };
    return { content: text.slice(sOff, eOff < 0 ? text.length : eOff), total };
}
function readFile(args) {
    const r = resolvePath(requireString(args, "path"));
    const start = optionalLine(args, "start_line");
    const end = optionalLine(args, "end_line");
    if (start !== undefined && end !== undefined && end < start)
        throw new result_js_1.ToolFail("end_line must be >= start_line");
    const text = readText(r);
    const { content, total } = lineSlice(text, start ?? 1, end ?? Number.MAX_SAFE_INTEGER);
    return { content: start === undefined && end === undefined ? text : content, total_lines: total };
}
function writeFile(args) {
    const r = resolvePath(requireString(args, "path"));
    const content = requireString(args, "content");
    const buf = Buffer.from(content, "utf8");
    try {
        fs.mkdirSync(path.dirname(r.real), { recursive: true });
        forget(r.real);
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
        throw new result_js_1.ToolFail("old_text must not be empty");
    const text = readText(r);
    const first = text.indexOf(oldText);
    if (first < 0)
        throw new result_js_1.ToolFail(`old_text not found in ${r.rel}`);
    let count = 1;
    for (let i = text.indexOf(oldText, first + oldText.length); i >= 0; i = text.indexOf(oldText, i + oldText.length)) {
        count++;
    }
    if (count > 1)
        throw new result_js_1.ToolFail(`old_text occurs ${count} times in ${r.rel}; it must occur exactly once`);
    const updated = text.slice(0, first) + newText + text.slice(first + oldText.length);
    try {
        forget(r.real);
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
            throw new result_js_1.ToolFail(`invalid regular expression: ${e.message}`);
        }
    }
}
// Lookaround can see past a line's ends, so for such patterns a whole-text match says nothing about single lines.
const LOOKAROUND = /\(\?<?[=!]/;
/** 1-based numbers of the lines of `text` that `re` matches, each line tested without its "\n" (and a trailing
 * "\r"), as in a plain line-by-line loop, in order, stopping after `limit`. */
function matchingLines(text, re, multi, limit) {
    const out = [];
    const len = text.length;
    let lineNo = 0; // number of the line starting at `counted`
    let counted = 0;
    const lineAt = (ls) => {
        for (let i = text.indexOf("\n", counted); i >= 0 && i < ls; i = text.indexOf("\n", i + 1))
            lineNo++;
        counted = ls;
        return lineNo + 1;
    };
    let q = 0;
    while (q < len && out.length < limit) {
        let ls = q;
        if (multi) {
            // Jump to the first line that can match. Every assertion except lookaround (excluded above) behaves the
            // same at a line's ends in the whole text with the m flag as in the line alone, so a line that matches on
            // its own always contains a whole-text match start, and lines before the leftmost match start cannot match.
            multi.lastIndex = q;
            const m = multi.exec(text);
            if (!m)
                break;
            ls = m.index === 0 ? 0 : text.lastIndexOf("\n", m.index - 1) + 1;
            if (ls < q)
                ls = q;
            if (ls >= len)
                break; // match after a final newline: not a line
        }
        let le = text.indexOf("\n", ls);
        if (le < 0)
            le = len;
        let line = text.slice(ls, le);
        if (line.endsWith("\r"))
            line = line.slice(0, -1);
        if (re.test(line))
            out.push({ line: lineAt(ls), text: line });
        q = le + 1;
    }
    return out;
}
async function search(args) {
    const pattern = requireString(args, "pattern");
    const re = compileRegex(pattern);
    const multi = LOOKAROUND.test(pattern) ? null : new RegExp(re.source, re.flags + "gm");
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
    const texts = await searchTexts(files);
    const matches = [];
    for (let i = 0; i < files.length; i++) {
        const text = texts[i];
        if (text === null)
            continue; // unreadable or not UTF-8: skipped
        for (const m of matchingLines(text, re, multi, MAX_MATCHES + 1 - matches.length)) {
            if (matches.length >= MAX_MATCHES)
                return { matches, truncated: true };
            matches.push({ path: files[i].path, line: m.line, text: m.text });
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
async function call(name, args) {
    const h = HANDLERS[name];
    if (!h)
        return (0, result_js_1.errorResult)(`unknown tool: ${name}`);
    try {
        return (0, result_js_1.okResult)(await h(args));
    }
    catch (e) {
        if (e instanceof result_js_1.ToolFail)
            return (0, result_js_1.errorResult)(e.message);
        return (0, result_js_1.errorResult)(e?.message ?? String(e));
    }
}
async function serveFiles(workspace) {
    WS = fs.realpathSync(path.resolve(workspace));
    const server = new index_js_1.Server({ name: "tinycoder-files", version: "1.0.0" }, { capabilities: { tools: {} }, jsonSchemaValidator: new validator_js_1.LazyValidator() });
    server.setRequestHandler(types_js_1.ListToolsRequestSchema, () => ({ tools: TOOLS }));
    server.setRequestHandler(types_js_1.CallToolRequestSchema, (req) => call(req.params.name, (req.params.arguments ?? {})));
    await server.connect(new stdio_js_1.StdioServerTransport());
}
