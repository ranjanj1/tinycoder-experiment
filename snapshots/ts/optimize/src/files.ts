// MCP server 1: `files` (SPEC section 2).
import * as fs from "node:fs";
import * as path from "node:path";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { LazyValidator } from "./validator.js";
import { errorResult, okResult, ToolFail, type ToolResult } from "./result.js";

const MAX_LIST = 500;
const MAX_MATCHES = 200;

const TOOLS = [
  {
    name: "list_files",
    description:
      "List files and directories recursively under a workspace-relative path (default \".\"), sorted by path. " +
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
    description:
      "Read a UTF-8 text file. Optionally only lines start_line..end_line (1-based, inclusive). " +
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
    description:
      "Replace exactly one occurrence of old_text with new_text in a file. " +
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
    description:
      "Search UTF-8 text files under a workspace-relative path (default \".\") for lines matching a regular " +
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

interface Resolved {
  real: string; // absolute real path to operate on
  rel: string; // workspace-relative display path (forward slashes, "." for the root)
}

function isOutside(rel: string): boolean {
  return rel === ".." || rel.startsWith(".." + path.sep) || path.isAbsolute(rel);
}

function hasGit(rel: string): boolean {
  return rel.split(path.sep).includes(".git");
}

function toDisplay(rel: string): string {
  if (rel === "") return ".";
  return path.sep === "/" ? rel : rel.split(path.sep).join("/");
}

/** Resolve a workspace-relative path, following symlinks of every existing part. */
function resolvePath(p: unknown, label = "path"): Resolved {
  if (p === undefined || p === null) p = ".";
  if (typeof p !== "string") throw new ToolFail(`${label} must be a string`);
  if (p === "") p = ".";
  const ps = p as string;
  if (ps.includes("\0")) throw new ToolFail(`invalid ${label}`);
  if (path.isAbsolute(ps)) throw new ToolFail(`absolute paths are not allowed: ${ps}`);
  const lexical = path.resolve(WS, ps);
  const lrel = path.relative(WS, lexical);
  if (isOutside(lrel)) throw new ToolFail(`path is outside the workspace: ${ps}`);
  if (hasGit(lrel)) throw new ToolFail(`path inside .git is not allowed: ${ps}`);

  // Follow symlinks: realpath the deepest existing ancestor, then re-append the missing tail.
  let cur = lexical;
  const tail: string[] = [];
  let real: string;
  for (;;) {
    try {
      real = fs.realpathSync(cur);
      break;
    } catch (e) {
      const code = (e as NodeJS.ErrnoException).code;
      if (code !== "ENOENT" && code !== "ENOTDIR") throw e;
      const parent = path.dirname(cur);
      if (parent === cur) throw new ToolFail(`cannot resolve path: ${ps}`);
      tail.unshift(path.basename(cur));
      cur = parent;
    }
  }
  if (tail.length) real = path.join(real, ...tail);
  const rrel = path.relative(WS, real);
  if (isOutside(rrel)) throw new ToolFail(`path resolves outside the workspace: ${ps}`);
  if (hasGit(rrel)) throw new ToolFail(`path inside .git is not allowed: ${ps}`);
  return { real, rel: toDisplay(lrel) };
}

function joinRel(base: string, name: string): string {
  return base === "." ? name : base + "/" + name;
}

/** Whether a symlink target stays inside the workspace and outside .git. */
function linkInside(abs: string): boolean {
  try {
    const r = path.relative(WS, fs.realpathSync(abs));
    return !isOutside(r) && !hasGit(r);
  } catch {
    return false;
  }
}

interface Entry {
  path: string;
  type: "file" | "dir";
  size: number;
  abs: string;
}

// ---- caches (per server process) ----
// Both are validated with stat on every use, so they never return stale data: a directory's listing is reused only
// while its inode, mtime and ctime (ns) are unchanged (adding, removing or renaming an entry changes them), and a
// file's text only while inode, size, mtime and ctime (ns) are unchanged (any write changes ctime, which user code
// cannot set). Our own writes also drop the entry. Memory is bounded by CACHE_BUDGET.
// Like git's "racily clean" rule, nothing is cached while its mtime or ctime is less than RACY_NS old: on a file
// system with coarse timestamps a second change in the same tick would keep them equal, but once an entry is older
// than any tick, every later change gives a different timestamp.

type Kind = "dir" | "file" | "link" | "other";
interface DirCached {
  ino: bigint;
  mtime: bigint;
  ctime: bigint;
  entries: { name: string; kind: Kind }[];
}
interface TextCached {
  ino: bigint;
  size: bigint;
  mtime: bigint;
  ctime: bigint;
  text: string | null; // null: not valid UTF-8
}
const dirCache = new Map<string, DirCached>();
const textCache = new Map<string, TextCached>();
const CACHE_MAX_FILE = 1 << 20; // bytes; larger files are read every time
const CACHE_BUDGET = 16 << 20; // total cached characters before the text cache is cleared
let cachedChars = 0;
const RACY_NS = 2_000_000_000n;

function settled(st: fs.BigIntStats): boolean {
  const now = BigInt(Date.now()) * 1_000_000n;
  return now - st.mtimeNs >= RACY_NS && now - st.ctimeNs >= RACY_NS;
}

function forget(abs: string): void {
  const c = textCache.get(abs);
  if (c) {
    cachedChars -= c.text?.length ?? 0;
    textCache.delete(abs);
  }
}

function listDir(absDir: string): { name: string; kind: Kind }[] | null {
  let st: fs.BigIntStats | undefined;
  try {
    st = fs.statSync(absDir, { bigint: true, throwIfNoEntry: false });
  } catch {
    st = undefined;
  }
  if (!st) {
    dirCache.delete(absDir);
    return null;
  }
  const c = dirCache.get(absDir);
  if (c && c.ino === st.ino && c.mtime === st.mtimeNs && c.ctime === st.ctimeNs) return c.entries;
  let dirents: fs.Dirent[];
  try {
    dirents = fs.readdirSync(absDir, { withFileTypes: true });
  } catch {
    dirCache.delete(absDir);
    return null;
  }
  const entries = dirents.map((d) => ({
    name: d.name,
    kind: (d.isDirectory() ? "dir" : d.isFile() ? "file" : d.isSymbolicLink() ? "link" : "other") as Kind,
  }));
  if (settled(st)) dirCache.set(absDir, { ino: st.ino, mtime: st.mtimeNs, ctime: st.ctimeNs, entries });
  else dirCache.delete(absDir);
  return entries;
}

/** Recursive walk. Does not descend into symlinked directories; skips links leaving the workspace. */
function walk(absDir: string, relDir: string, out: Entry[], filesOnly: boolean): void {
  const entries = listDir(absDir);
  if (!entries) return;
  for (const d of entries) {
    if (d.name === ".git") continue;
    const abs = path.join(absDir, d.name);
    const rel = joinRel(relDir, d.name);
    if (d.kind === "dir") {
      if (!filesOnly) out.push({ path: rel, type: "dir", size: 0, abs });
      walk(abs, rel, out, filesOnly);
    } else if (d.kind === "file") {
      let size = 0;
      if (!filesOnly) {
        const st = fs.statSync(abs, { throwIfNoEntry: false });
        if (!st) continue;
        size = st.size;
      }
      out.push({ path: rel, type: "file", size, abs });
    } else if (d.kind === "link") {
      if (!linkInside(abs)) continue;
      let st: fs.Stats | undefined;
      try {
        st = fs.statSync(abs, { throwIfNoEntry: false });
      } catch {
        continue;
      }
      if (!st) continue;
      if (st.isDirectory()) {
        if (!filesOnly) out.push({ path: rel, type: "dir", size: 0, abs });
      } else if (st.isFile()) {
        out.push({ path: rel, type: "file", size: st.size, abs });
      }
    }
  }
}

/** Text of each file for search (null: unreadable or not UTF-8). Cache hits need one stat; misses are read in
 * parallel on libuv's thread pool. */
async function searchTexts(files: Entry[]): Promise<(string | null)[]> {
  const texts: (string | null)[] = new Array(files.length).fill(null);
  const misses: Promise<void>[] = [];
  for (let i = 0; i < files.length; i++) {
    const abs = files[i].abs;
    let st: fs.BigIntStats | undefined;
    try {
      st = fs.statSync(abs, { bigint: true, throwIfNoEntry: false });
    } catch {
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
    misses.push(
      new Promise<void>((resolve) => {
        fs.readFile(abs, (err, buf) => {
          if (err) {
            forget(abs);
            return resolve();
          }
          let text: string | null;
          try {
            text = decoder.decode(buf);
          } catch {
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
      }),
    );
  }
  await Promise.all(misses);
  return texts;
}

function byPath(a: Entry, b: Entry): number {
  return a.path < b.path ? -1 : a.path > b.path ? 1 : 0;
}

function statOrFail(r: Resolved): fs.Stats {
  try {
    return fs.statSync(r.real);
  } catch (e) {
    throw fsFail(e, r.rel);
  }
}

function fsFail(e: unknown, rel: string): ToolFail {
  if (e instanceof ToolFail) return e;
  const code = (e as NodeJS.ErrnoException).code;
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

function readText(r: Resolved): string {
  let buf: Buffer;
  try {
    buf = fs.readFileSync(r.real);
  } catch (e) {
    throw fsFail(e, r.rel);
  }
  try {
    return decoder.decode(buf);
  } catch {
    throw new ToolFail(`file is not valid UTF-8: ${r.rel}`);
  }
}

function requireString(args: Record<string, unknown>, key: string): string {
  const v = args[key];
  if (typeof v !== "string") throw new ToolFail(`${key} is required and must be a string`);
  return v;
}

function optionalLine(args: Record<string, unknown>, key: string): number | undefined {
  const v = args[key];
  if (v === undefined || v === null) return undefined;
  if (typeof v !== "number" || !Number.isInteger(v) || v < 1) throw new ToolFail(`${key} must be an integer >= 1`);
  return v;
}

// ---- tools ----

function listFiles(args: Record<string, unknown>): unknown {
  const r = resolvePath(args.path);
  const st = statOrFail(r);
  const out: Entry[] = [];
  if (st.isDirectory()) {
    walk(r.real, r.rel, out, false);
  } else {
    out.push({ path: r.rel, type: "file", size: st.size, abs: r.real });
  }
  out.sort(byPath);
  const entries = out.slice(0, MAX_LIST).map((e) => ({ path: e.path, type: e.type, size: e.size }));
  if (out.length > MAX_LIST) return { entries, truncated: true };
  return { entries };
}

/** Lines are split on "\n" and keep it; a trailing newline does not start a new line. Returns the slice from line
 * `start` to line `end` (1-based, inclusive, clamped) and the line count, without building a line array. */
function lineSlice(text: string, start: number, end: number): { content: string; total: number } {
  let total = 0;
  let pos = 0;
  let sOff = start === 1 ? 0 : -1;
  let eOff = -1;
  for (;;) {
    const i = text.indexOf("\n", pos);
    if (i < 0) break;
    total++;
    pos = i + 1;
    if (total === start - 1) sOff = pos;
    if (total === end) eOff = pos;
  }
  if (pos < text.length) total++;
  if (sOff < 0) return { content: "", total };
  return { content: text.slice(sOff, eOff < 0 ? text.length : eOff), total };
}

function readFile(args: Record<string, unknown>): unknown {
  const r = resolvePath(requireString(args, "path"));
  const start = optionalLine(args, "start_line");
  const end = optionalLine(args, "end_line");
  if (start !== undefined && end !== undefined && end < start) throw new ToolFail("end_line must be >= start_line");
  const text = readText(r);
  const { content, total } = lineSlice(text, start ?? 1, end ?? Number.MAX_SAFE_INTEGER);
  return { content: start === undefined && end === undefined ? text : content, total_lines: total };
}

function writeFile(args: Record<string, unknown>): unknown {
  const r = resolvePath(requireString(args, "path"));
  const content = requireString(args, "content");
  const buf = Buffer.from(content, "utf8");
  try {
    fs.mkdirSync(path.dirname(r.real), { recursive: true });
    forget(r.real);
    fs.writeFileSync(r.real, buf);
  } catch (e) {
    throw fsFail(e, r.rel);
  }
  return { bytes_written: buf.length };
}

function editFile(args: Record<string, unknown>): unknown {
  const r = resolvePath(requireString(args, "path"));
  const oldText = requireString(args, "old_text");
  const newText = requireString(args, "new_text");
  if (oldText === "") throw new ToolFail("old_text must not be empty");
  const text = readText(r);
  const first = text.indexOf(oldText);
  if (first < 0) throw new ToolFail(`old_text not found in ${r.rel}`);
  let count = 1;
  for (let i = text.indexOf(oldText, first + oldText.length); i >= 0; i = text.indexOf(oldText, i + oldText.length)) {
    count++;
  }
  if (count > 1) throw new ToolFail(`old_text occurs ${count} times in ${r.rel}; it must occur exactly once`);
  const updated = text.slice(0, first) + newText + text.slice(first + oldText.length);
  try {
    forget(r.real);
    fs.writeFileSync(r.real, updated, "utf8");
  } catch (e) {
    throw fsFail(e, r.rel);
  }
  return { replacements: 1 };
}

function compileRegex(pattern: string): RegExp {
  try {
    return new RegExp(pattern, "u");
  } catch {
    try {
      return new RegExp(pattern);
    } catch (e) {
      throw new ToolFail(`invalid regular expression: ${(e as Error).message}`);
    }
  }
}

// Lookaround can see past a line's ends, so for such patterns a whole-text match says nothing about single lines.
const LOOKAROUND = /\(\?<?[=!]/;

/** 1-based numbers of the lines of `text` that `re` matches, each line tested without its "\n" (and a trailing
 * "\r"), as in a plain line-by-line loop, in order, stopping after `limit`. */
function matchingLines(text: string, re: RegExp, multi: RegExp | null, limit: number): { line: number; text: string }[] {
  const out: { line: number; text: string }[] = [];
  const len = text.length;
  let lineNo = 0; // number of the line starting at `counted`
  let counted = 0;
  const lineAt = (ls: number): number => {
    for (let i = text.indexOf("\n", counted); i >= 0 && i < ls; i = text.indexOf("\n", i + 1)) lineNo++;
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
      if (!m) break;
      ls = m.index === 0 ? 0 : text.lastIndexOf("\n", m.index - 1) + 1;
      if (ls < q) ls = q;
      if (ls >= len) break; // match after a final newline: not a line
    }
    let le = text.indexOf("\n", ls);
    if (le < 0) le = len;
    let line = text.slice(ls, le);
    if (line.endsWith("\r")) line = line.slice(0, -1);
    if (re.test(line)) out.push({ line: lineAt(ls), text: line });
    q = le + 1;
  }
  return out;
}

async function search(args: Record<string, unknown>): Promise<unknown> {
  const pattern = requireString(args, "pattern");
  const re = compileRegex(pattern);
  const multi = LOOKAROUND.test(pattern) ? null : new RegExp(re.source, re.flags + "gm");
  const r = resolvePath(args.path);
  const st = statOrFail(r);
  const files: Entry[] = [];
  if (st.isDirectory()) {
    walk(r.real, r.rel, files, true);
    files.sort(byPath);
  } else {
    files.push({ path: r.rel, type: "file", size: st.size, abs: r.real });
  }
  const texts = await searchTexts(files);
  const matches: { path: string; line: number; text: string }[] = [];
  for (let i = 0; i < files.length; i++) {
    const text = texts[i];
    if (text === null) continue; // unreadable or not UTF-8: skipped
    for (const m of matchingLines(text, re, multi, MAX_MATCHES + 1 - matches.length)) {
      if (matches.length >= MAX_MATCHES) return { matches, truncated: true };
      matches.push({ path: files[i].path, line: m.line, text: m.text });
    }
  }
  return { matches };
}

const HANDLERS: Record<string, (a: Record<string, unknown>) => unknown | Promise<unknown>> = {
  list_files: listFiles,
  read_file: readFile,
  write_file: writeFile,
  edit_file: editFile,
  search,
};

async function call(name: string, args: Record<string, unknown>): Promise<ToolResult> {
  const h = HANDLERS[name];
  if (!h) return errorResult(`unknown tool: ${name}`);
  try {
    return okResult(await h(args));
  } catch (e) {
    if (e instanceof ToolFail) return errorResult(e.message);
    return errorResult((e as Error)?.message ?? String(e));
  }
}

export async function serveFiles(workspace: string): Promise<void> {
  WS = fs.realpathSync(path.resolve(workspace));
  const server = new Server({ name: "tinycoder-files", version: "1.0.0" }, { capabilities: { tools: {} }, jsonSchemaValidator: new LazyValidator() });
  server.setRequestHandler(ListToolsRequestSchema, () => ({ tools: TOOLS }));
  server.setRequestHandler(CallToolRequestSchema, (req) =>
    call(req.params.name, (req.params.arguments ?? {}) as Record<string, unknown>),
  );
  await server.connect(new StdioServerTransport());
}
