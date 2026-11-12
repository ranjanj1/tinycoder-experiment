// Build step: pack dist/main.js and every CommonJS module it statically requires (our code and the CJS builds of
// the allowed npm packages, unchanged) into one file per command, dist/tinycoder-<files|shell|run>.js.
//
// Why: startup is dominated by Node resolving, stat-ing, reading and compiling 250-550 small files. One file is
// read once and, through bin/tinycoder, compiled from a V8 code cache (src/build/warm.ts).
//
// How: each module becomes a factory `function (exports, require, module, __filename, __dirname) { <source> }`,
// exactly the wrapper Node itself uses for CommonJS. It runs only when first required, as in Node. `require` calls
// with string literals are resolved at build time with Node's own resolver (createRequire(file).resolve).
// Builtins go to the host `require`; anything else (non-literal or unresolvable specifiers) goes to the real
// `require` of the original file at run time, so behaviour matches the unbundled program. `__filename` and
// `__dirname` are the original file locations, relative to the install root, so the bundle moves with the
// workspace.
//
// Deferred packages: a few dependencies that the MCP SDK requires at load time but only uses inside functions
// tinycoder never calls on its normal path (ajv + ajv-formats: JSON Schema validation, used only through
// src/validator.ts; zod-to-json-schema: converting zod schemas, used only by McpServer). `require()` of these
// returns a proxy that loads the real module on first access (any property read, `in`, key listing, write, ...),
// so behaviour is the same when they are used; they just don't run otherwise. The one thing answered without
// loading is the `__esModule` flag that TypeScript's import helpers read at load time, and only when the build
// verified that the module sets it to true.
import * as fs from "node:fs";
import * as path from "node:path";
import { builtinModules, createRequire } from "node:module";

const ROOT = path.resolve(__dirname, "..", "..");
const DIST = path.join(ROOT, "dist");

const BUILTINS = new Set(builtinModules);
const REQUIRE_RE = /(?<![\w$.])require\(\s*(['"])([^'"\\\n]+)\1\s*\)/g;
const DEFERRED = new Set(["ajv", "ajv-formats", "zod-to-json-schema"]);
const ES_MODULE_FLAG = /Object\.defineProperty\(exports, "__esModule", \{ value: true \}\)/;

interface Mod {
  id: number;
  file: string;
  deps: Record<string, number>; // module id, or -1 for a builtin
  deferred: Record<string, boolean>; // spec -> verified `__esModule === true`
  body: string;
}

function isBuiltin(spec: string): boolean {
  return spec.startsWith("node:") || BUILTINS.has(spec) || BUILTINS.has(spec.split("/")[0]);
}

function isCommonJS(file: string): boolean {
  if (file.endsWith(".cjs") || file.endsWith(".json")) return true;
  if (file.endsWith(".mjs") || !file.endsWith(".js")) return false;
  // The nearest package.json decides, as in Node.
  for (let dir = path.dirname(file); ; dir = path.dirname(dir)) {
    const pj = path.join(dir, "package.json");
    if (fs.existsSync(pj)) {
      try {
        return (JSON.parse(fs.readFileSync(pj, "utf8")) as { type?: string }).type !== "module";
      } catch {
        return true;
      }
    }
    if (path.dirname(dir) === dir) return true;
  }
}

function collect(entry: string, exclude: Set<string>): Mod[] {
  const byFile = new Map<string, Mod>();
  const mods: Mod[] = [];
  const queue: Mod[] = [];
  const add = (file: string): Mod => {
    let m = byFile.get(file);
    if (!m) {
      m = { id: mods.length, file, deps: {}, deferred: {}, body: "" };
      mods.push(m);
      byFile.set(file, m);
      queue.push(m);
    }
    return m;
  };
  add(entry);
  while (queue.length) {
    const m = queue.shift()!;
    let src = fs.readFileSync(m.file, "utf8");
    if (m.file.endsWith(".json")) {
      m.body = `module.exports = ${src.trim()};`;
      continue;
    }
    if (src.startsWith("#!")) src = "//" + src.slice(2);
    m.body = src;
    const req = createRequire(m.file);
    for (const match of src.matchAll(REQUIRE_RE)) {
      const spec = match[2];
      if (spec in m.deps) continue;
      if (isBuiltin(spec)) {
        m.deps[spec] = -1;
        continue;
      }
      let resolved: string;
      try {
        resolved = req.resolve(spec);
      } catch {
        continue; // left to the real require at run time (it may never run)
      }
      if (!path.isAbsolute(resolved) || !isCommonJS(resolved) || exclude.has(resolved)) continue;
      const dep = add(resolved);
      m.deps[spec] = dep.id;
      if (DEFERRED.has(spec)) m.deferred[spec] = ES_MODULE_FLAG.test(fs.readFileSync(resolved, "utf8"));
    }
  }
  return mods;
}

// Runtime, inside the bundle's outer function.
const RUNTIME = `
const require_ = require;
const __path = require_("node:path");
const __root = __path.resolve(__dirname, "..");
const __cache = new Array(__M.length);
const __proxies = new Map();
function __defer(id, esModule) {
  let p = __proxies.get(id);
  if (p) return p;
  let real;
  const load = () => (real ??= __load(id));
  p = new Proxy({}, {
    get: (_, k) => (k === "__esModule" && real === undefined && esModule ? true : Reflect.get(load(), k)),
    has: (_, k) => Reflect.has(load(), k),
    ownKeys: () => Reflect.ownKeys(load()),
    getOwnPropertyDescriptor: (_, k) => {
      const d = Reflect.getOwnPropertyDescriptor(load(), k);
      if (d) d.configurable = true;
      return d;
    },
    set: (_, k, v) => Reflect.set(load(), k, v),
    defineProperty: (_, k, d) => Reflect.defineProperty(load(), k, d),
    deleteProperty: (_, k) => Reflect.deleteProperty(load(), k),
    getPrototypeOf: () => Reflect.getPrototypeOf(load()),
  });
  __proxies.set(id, p);
  return p;
}
function __load(id) {
  const cached = __cache[id];
  if (cached !== undefined) return cached.exports;
  const def = __M[id];
  const filename = __path.join(__root, def[2]);
  const module = { exports: {}, id: filename, filename, loaded: false, children: [], paths: [], parent: null };
  __cache[id] = module;
  let real;
  const realRequire = () => (real ??= require_("node:module").createRequire(filename));
  const deps = def[1];
  const deferred = def[3];
  const req = function require(spec) {
    const d = deps[spec];
    if (d === -1) return require_(spec);
    if (d !== undefined) return deferred !== undefined && spec in deferred ? __defer(d, deferred[spec]) : __load(d);
    return realRequire()(spec);
  };
  req.resolve = (spec, opts) => realRequire().resolve(spec, opts);
  req.cache = require_.cache;
  req.main = undefined;
  def[0].call(module.exports, module.exports, req, module, filename, __path.dirname(filename));
  module.loaded = true;
  return module.exports;
}
module.exports = __load(0);
`;

function write(out: string, mods: Mod[]): void {
  // The file is one function expression with Node's CommonJS wrapper parameters, so bin/tinycoder compiles it as
  // is (no second copy of a multi-megabyte string just to add the wrapper).
  const parts: string[] = [
    "(function (exports, require, module, __filename, __dirname) {// Generated by src/build/bundle.ts. Do not edit.\nconst __M = [",
  ];
  for (const m of mods) {
    const rel = path.relative(ROOT, m.file).split(path.sep).join("/");
    const deferred = Object.keys(m.deferred).length ? `, ${JSON.stringify(m.deferred)}` : "";
    parts.push(
      `[function (exports, require, module, __filename, __dirname) {\n${m.body}\n}, ${JSON.stringify(m.deps)}, ${JSON.stringify(rel)}${deferred}],`,
    );
  }
  parts.push("];", RUNTIME, "})");
  fs.writeFileSync(out, parts.join("\n"));
  const nDeferred = mods.reduce((n, m) => n + Object.keys(m.deferred).length, 0);
  process.stderr.write(
    `bundle: ${mods.length} modules (${nDeferred} deferred requires) -> ${path.relative(ROOT, out)} (${fs.statSync(out).size} bytes)\n`,
  );
}

// One bundle per command, so a server never parses the agent's dependencies (and the reverse). Each starts at
// dist/main.js; the other commands' modules are left out (main.js only requires them when that command runs).
const own = (n: string) => path.join(DIST, n);
const COMMANDS: Record<string, string[]> = {
  files: ["shell.js", "agent.js"],
  shell: ["files.js", "agent.js"],
  run: ["files.js", "shell.js"],
};
for (const [cmd, skip] of Object.entries(COMMANDS)) {
  write(own(`tinycoder-${cmd}.js`), collect(own("main.js"), new Set(skip.map(own))));
}
