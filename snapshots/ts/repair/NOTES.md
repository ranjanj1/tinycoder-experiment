# tinycoder, lane TS: notes

## Result
`python3 tests/check.py bin/tinycoder` → **11/11 passed** (files_tools_listed, shell_tools_listed, list_and_read,
write_and_edit, search_regex, files_refuse_escapes, shell_tests_and_commands, agent_basic_loop, agent_hook_blocks,
agent_step_limit, agent_retries_overloaded).

I did not read the sources of `tests/` (check.py, fake_api.py, mcp_client.py, bench_dev.py, task files). I only
ran check.py and bench_dev.py. Performance work and its numbers: see "Performance round" below.

## Commands I ran (in order)
```sh
node --version; npm --version; npm config list           # Node 26.13.0, npm 11.20.0, offline cache
npm install --offline                                      # created package-lock.json from the kit's cache
npx tsc -p tsconfig.json                                   # TypeScript 7.0.2
python3 tests/check.py bin/tinycoder                       # visible checks
# my own scratch tests, all in /tmp/tcscratch (outside the workspace):
node t.mjs      # MCP client against both servers: symlink escapes, .git, absolute paths, ranges, non-UTF-8, timeouts
node t2.mjs     # list (500) / search (200) truncation, 8000-character output tail
node ft.mjs ... # agent loop with globalThis.fetch swapped for an in-process fake (retries, hooks, thinking
                # blocks, max_tokens, HTTP 400, connection errors, step limit)
python3 -I st.py ...   # startup time to the initialize reply, and child maxrss
npm ci --offline && npx tsc -p tsconfig.json   # clean rebuild from a copy in /tmp/tcclean
# performance round (all scratch files in /tmp/tcscratch):
python3 tests/bench_dev.py bin/tinycoder       # before and after every change below
node lat.mjs / python3 -I pyrt.py ...          # per-call latency on a 349-file / 3 MB tree
node fuzz.mjs <seed>                           # differential test, old build vs new files server (below)
node stale.mjs                                 # cache invalidation after external writes
node defer-test.js                             # deferred ajv / zod-to-json-schema still work when used
npm run build                                  # = tsc + node dist/build/bundle.js + node dist/build/warm.js
# clean-build check: copy without dist/ node_modules/ .npm-cache/, run the BUILD.md block, then serve + check.py
```
Loopback TCP connections are blocked in this sandbox (even with the sandbox disabled), so I tested the agent loop
by replacing `fetch` in-process instead of with an HTTP fake.

## Layout
- `src/main.ts`: CLI (`serve files`, `serve shell`, `run`) using `node:util` `parseArgs`. It `require`s only the
  module the command needs, so the servers never load the Anthropic SDK. Everything compiles to CommonJS.
- `src/files.ts`: MCP server `files`. `src/shell.ts`: MCP server `shell`.
- `src/agent.ts`: `run`. It spawns both servers and is an MCP client over stdio. It also holds the hooks, the
  model loop, the runs log and the final JSON.
- `src/allow.ts`: the `run_command` allowlist, used by both the shell server and the pre-call hook.
- `src/result.ts`: tool result helpers and the last-N-characters tail.
- `src/validator.ts`: lazy JSON Schema validator passed to the SDK's `Server`/`Client` (`jsonSchemaValidator`).
- `src/build/bundle.ts`, `src/build/warm.ts`: build steps that write `dist/tinycoder-<cmd>.js` and `.cache`.
- `bin/tinycoder`: `#!/usr/bin/env -S node --max-semi-space-size=1`. It picks the bundle for the command and runs
  it through `vm.Script` with its V8 code cache. If the bundle is missing, it falls back to `dist/main.js`.

## Design
- **MCP**: both servers use the SDK's low-level `Server` (`@modelcontextprotocol/sdk/server/index.js`) with
  `setRequestHandler(ListToolsRequestSchema | CallToolRequestSchema)` and `StdioServerTransport`. The agent uses the
  SDK's `Client` and `StdioClientTransport`. I wrote no protocol code myself.
  - I used the low-level `Server` rather than `McpServer` + zod for two reasons. The input schemas can be plain
    JSON Schema exactly as I want them listed (e.g. `minimum: 1`, `minItems: 1`). And every bad-argument case goes
    through my own validation, so the error text always starts with `error: `. McpServer's zod validation errors
    would start with `MCP error -32602`.
- `run` spawns the servers as `process.execPath <its own V8 flags> bin/tinycoder serve …` (same flags, so the
  servers can use their code caches), with the parent's full environment, so the
  shell server can pass `PATH`, `HOME` and `LANG` on to commands. It connects to both in parallel while it loads the
  Anthropic SDK.
- **Model calls**: official `@anthropic-ai/sdk` (1.8.0) `messages.create` with exactly `model`, `max_tokens`,
  `system`, `messages`, `tools` and `output_config`. `anthropic-version: 2026-10-21` is also set in `defaultHeaders`
  (the SDK already sends that value). SDK retries are off (`maxRetries: 0`). My own loop retries HTTP
  429/500/502/503/529 and `APIConnectionError` (which includes timeouts), waiting 1, 2 and 4 s, for 4 attempts in
  all. I did not use the SDK's retry because it also retries 408/409, honours `retry-after` and adds jitter, which the
  spec does not allow.
- The assistant `content` from the SDK is appended to `messages` as is, thinking blocks included.
- The runs file is written with `appendFileSync`, one JSON line per event, in order. Stdout gets exactly one line,
  written with `fs.writeSync(1, …)` so it is not lost when the process exits.

## Choices where SPEC.md is silent
**files server**
- Path check: absolute → reject. The lexical path (`path.resolve(ws, p)`) must stay inside the workspace. Then the
  real path is checked: `realpath` of the deepest existing ancestor, with the missing tail re-appended (needed for
  `write_file` to new paths). Any `.git` component, at any depth and including `.git` itself, is rejected in either
  form. `..` that stays inside the workspace is allowed by the server (the agent's hook still blocks it). An empty
  or missing `path` means `.`.
- `list_files`: entry paths are relative to the workspace, not to the `path` argument, with `/` separators.
  Directories have `size: 0`. Symlinks are listed as their target's type, but symlinked directories are not walked
  into, and links that resolve outside the workspace (or into `.git`) are left out. All entries are collected and
  sorted by full path string (UTF-16 code-unit order), then cut to 500. `truncated` appears only when it is true.
  If `path` is a file, the result is that one entry.
- `read_file`: lines are split on `\n` and keep their terminators, so `content` is an exact slice of the file. A
  trailing newline does not start an extra line; an empty file has 0 lines. With no range the whole file is returned.
  Only `end_line` given → start at 1. `end_line` past the end is clamped. `start_line` past the end gives `""`.
  `end_line < start_line`, or non-integer / < 1 values, are errors. A UTF-8 BOM is kept.
- `write_file`: `bytes_written` is the UTF-8 byte length. `edit_file`: occurrences are counted without overlap;
  empty `old_text` is an error; the replacement is literal (no `$&` patterns).
- `search`: JavaScript `RegExp`, tried first with the `u` flag and then without it. It runs on each line without
  its `\n` (and a trailing `\r`). `text` is that line. Files are visited in sorted path order. If `path` is a file,
  only that file is searched. Unreadable and non-UTF-8 files are skipped. `truncated: true` is set only when a
  201st match exists.
- Node error messages are mapped to short relative-path messages (e.g. `error: no such file or directory: x`), so
  absolute paths are not leaked.

**shell server**
- `task.json` is read on each `run_tests` call. A missing or invalid file is a tool error.
- No shell. `stdin` is `/dev/null`. Each child runs in its own process group (`detached`), and on timeout the
  whole group gets SIGKILL, so grandchildren die too. `exit_code` is -1 on timeout and also when the process
  dies from a signal. If the pipes are still open 2 s after the kill, the result is returned anyway.
- A command that cannot be started (e.g. ENOENT) is a tool error, not an exit code.
- `output` is stdout and stderr in the order the chunks arrived. "Last 8,000 characters" counts Unicode code points
  (surrogate pairs are not split). Output is decoded as UTF-8 with replacement characters. While a command runs,
  each stream keeps only its last ~256 KiB.
- Only `PATH`, `HOME` and `LANG` are passed, and only those that are set in the server's environment.
- `argv[0]` must be exactly an allowlisted name (`/bin/ls` is rejected). `git` needs a second argument from the
  list.

**agent**
- Hook reasons: `unknown tool: X`, `absolute path not allowed: P` (POSIX-absolute, `\`-rooted or `C:`-style),
  `path contains '..': P` (split on `/` and `\`), `write_file content is N bytes, over the 100000 byte limit`, or the
  allowlist reason. Only a string `path` argument is checked, on any tool.
- `tool_result.content` is the server's text blocks joined with `\n`, as a string. `is_error: true` is sent only for
  errors (the field is left out on success). A transport failure on a tool call becomes `error: <message>`, and the
  loop goes on.
- `steps` counts a request even when it fails. If the 20th response is `tool_use`, its tools still run, then the run
  ends with `step_limit` (reason `"step limit of 20 reached"`).
- `reason`: `null` for `done`; the stop reason for an unexpected `stop_reason`; `api error <status>: <message>` or the
  SDK's message for API or connection failures.
- `final_text`: the text blocks of the last assistant message received, joined with `\n`. It is `""` if there is
  none. For `error` and `step_limit` it is the last message's text, which can be empty.
- `duration_ms` in an `llm` line covers the whole step, retries and backoff included. `llm_ms` and `tool_ms` are
  sums of those durations. `wall_ms` is `performance.now()`, i.e. measured from process start. There is no `llm`
  line for a step whose request failed in the end. `cache_read_input_tokens` is 0 when the API returns null or
  leaves it out. `input_tokens` in the final object is the sum of `usage.input_tokens` only.
- The run-id fallback is `Date.now()` at the start of `run`. `--runs` is created if it is missing.
- Any setup failure (missing TASK.md, a server failing to start, …) still prints the final JSON with status `error`
  and exits 1.

## Performance notes (first build)
- `--max-semi-space-size=1` lowers peak RSS per process by about 7 MB, with no measurable cost in time.
  `--single-threaded` together with it makes Node exit before serving stdio, so it is not used.
- Dropping `commander` for `util.parseArgs` saved a little startup.

## Performance round
All numbers are from `python3 tests/bench_dev.py bin/tinycoder` on this machine (typical of 2–3 runs; ±5 ms
startup noise, and the agent's wall time varies ±40 ms with the task's Python test runs).

| | before | after |
|---|---|---|
| startup to `initialize`, files / shell (ms) | 121–129 / 122–126 | 80–82 / 80–81 |
| peak RSS, files / shell (MB) | 65.0 / 63.5 | 62.5 / 58.3 |
| agent: wall ms (12 steps) / largest process MB | 637–665 / 96.7 | 514–522 / 94.8 |
| per call, read_file / search / run_command (µs) | 580 / 6,200 / 1,800–2,050 | 570 / 6,000 / 1,850 |

For reference, a bare `node hello.js` takes ~63 ms here, and Node flags don't change that (I tried ten). So
server startup is now ~18 ms above Node's own floor.

**Changes, in order of effect**
1. **Lazy JSON Schema validator** (`src/validator.ts`). The SDK's `Server` and `Client` constructors build an Ajv
   instance with formats eagerly. tinycoder never uses it: no tool declares an `outputSchema`, and nothing elicits
   input. I pass the SDK's own `jsonSchemaValidator` option with a wrapper that builds `AjvJsonSchemaValidator`
   on first use. Startup −12 ms. Validation behaviour is unchanged.
2. **One bundle per command** (`src/build/bundle.ts`). This is a small CommonJS bundler, using only the Node
   stdlib, that packs our code plus the CJS builds of the SDKs, unchanged, into one file. Each module keeps Node's
   own wrapper and runs only when first required. Literal `require`s are resolved at build time with Node's resolver,
   anything else falls back to the real `require`, and `__filename`/`__dirname` stay the original paths. On its own
   this did little; its value is enabling 3.
3. **V8 code cache taken after a warm-up run** (`src/build/warm.ts` + `bin/tinycoder`). The launcher compiles the
   bundle with `vm.Script` and `cachedData`. At build time each command runs once, offline, and
   `script.createCachedData()` is saved at exit. That cache holds every function compiled during the run, not
   only the top level, which is what Node's own compile cache stores. Startup −20 ms. A rejected or missing cache
   only means normal compilation. I checked that the agent and both servers it spawns all accept their caches.
4. **Bundle file carries its own wrapper.** That avoids a second multi-MB string, which had made bundling cost
   memory: peak RSS −4 MB (servers) and −8 MB (agent) against the unwrapped bundle.
5. **Deferred packages in the bundle.** `ajv`, `ajv-formats` and `zod-to-json-schema` are required by the MCP SDK
   at load time but used only inside functions tinycoder doesn't call. `require()` returns a proxy that loads the
   real module on first access. Only `__esModule` is answered without loading, and only after the build verified
   the module sets it. Startup −4 ms, RSS −1 MB. `defer-test.js` builds a real Ajv validator, with formats,
   through the SDK and converts a zod v3 schema through zod-to-json-schema inside the bundle: both work.
6. **Agent: one-time work moved into the server boot window.** TASK.md and system.txt are read before spawning
   (a missing task fails fast). Then, while the servers boot, the Anthropic SDK is loaded and Node is made to load
   its lazy `fetch` implementation (undici, ~40 ms on first use; nothing is sent). The first model call dropped
   from ~26 ms to ~14.5 ms. Fixed agent startup is down from ~139 to ~128 ms (`wall_ms` of a run that stops at the
   first request).
7. **`search`**: (a) A file's text is cached per server process. Each use first checks `stat` (inode, size,
   mtime, ctime in ns); the walk's directory listings are cached the same way, keyed on the directory's inode,
   mtime and ctime. (b) Files not in the cache are read in parallel on libuv's thread pool. (c) Instead of testing
   every line, a multiline (`gm`) search over the whole text jumps to the first line that can match, and that line
   is then tested exactly as before. This is equivalent for every assertion except lookaround (lookaround patterns
   use the old line loop): `^`, `$`, `\b` and `\B` behave the same at a line's ends in the whole text as in the
   line alone, so a matching line always contains the leftmost whole-text match start at or after it. On a
   349-file tree, search went from ~8.5 ms to ~1.5–2.5 ms server-side, and `list_files` from 1.7 to 1.3 ms.
   - Cache safety: a write changes ctime, which user code can't set. Our own writes also drop the entry.
     As in git's "racily clean" rule, nothing is cached while its mtime or ctime is less than 2 s old, so
     filesystems with coarse timestamps can't hide a second write in the same tick. The budget is 16 MB of text,
     and files over 1 MB are never cached.
8. **`read_file` with a range** finds line offsets with `indexOf` instead of building an array of every line
   (that took 14% of server time on a big file).

**What the benchmark's per-call numbers show.** Inside the server, a `read_file` takes ~150 µs and the
benchmark's search ~0.85 ms, from message received to reply written. Its search returns 200 matches, ~12 KB. A raw
pipe round trip of 17 KB takes ~23 µs. So most of the benchmark's 570 µs and 6 ms is spent on the client side,
growing with response size, and the response format is fixed by SPEC.md. That is why items 7–8 show clearly on my
own latency tests but barely in `call_us`. `run_command` is dominated by `fork/exec` of the program (~1.4 ms for
`ls` here). `detached` and PATH lookup made no difference.

**Tried and rejected**
- A V8 startup snapshot (`node --build-snapshot` from the bundle). It gave startup ~78 ms, but RSS was 88 MB
  because the 17 MB blob replaces Node's own snapshot.
- Compiling every module eagerly (parenthesized factories). It wins less than 3 and compiles code that never runs
  (agent RSS +9 MB).
- V8 flags `--no-opt --no-maglev` (−5 MB, but slower calls), plus `--optimize-for-size`, `--max-lazy`,
  `--lite-mode`, `--single-threaded-gc`, `--v8-pool-size`, `--no-sparkplug` and larger semi-spaces. None helped
  overall.
- Deferring `@opentelemetry/api` in the agent: the Anthropic SDK uses it on every request.

**Tests for this round**
- `tests/check.py` passes 11/11 after every change: on the full build, on a clean copy, and with only `tsc` run.
- Differential fuzz (`fuzz.mjs`) runs the pre-change build and the new files server side by side on a random
  workspace. Contents include CRLF, `\r` mid-line, U+2028, a BOM, emoji, invalid UTF-8, missing final newlines,
  empty files and nested directories. Patterns include `^`, `$`, `^$`, `\b`, `\B`, all four lookarounds, `\s`,
  `\n`, `\r`, empty, `x?`, backreferences, alternation and Unicode. Paths and line ranges are random. Between
  calls the fuzzer rewrites files externally, including same-size rewrites. In 7 of the 13 seeds it also pauses
  past the 2 s racy window so the caches are really used. 13 seeds, ~4,400 calls: 0 differences.
- `stale.mjs`: after a cache fill, a same-size rewrite, a new file in a cached directory and a deleted file are
  all seen by the very next call.
- Earlier scratch suites re-run on the final build: server edge cases, truncation, and agent paths (retry, hooks,
  thinking blocks, max_tokens, HTTP 400, step limit, missing TASK.md).

**Clean-build failure, now fixed.** After the bundling change, BUILD.md still listed only `npm ci` and `tsc`,
while the launcher needed `dist/tinycoder-*.js`. So a fresh copy built per BUILD.md failed with ENOENT before the
`initialize` reply. Fixes:
- BUILD.md now lists all four steps (`npm ci --offline`, `npx tsc -p tsconfig.json`, `node dist/build/bundle.js`,
  `node dist/build/warm.js`), and `npm run build` runs the last three.
- `bin/tinycoder` falls back to `dist/main.js` when a bundle is missing.

I verified it on a fresh copy without `dist/`, `node_modules/` or `.npm-cache/`. I ran exactly the BUILD.md code
block, extracted by script. Then `serve files` and `serve shell` answered `initialize`, `check.py` passed 11/11,
and the benchmark matched the numbers above. With the bundles removed (only steps 1–2), the fallback also
answers `initialize` and passes 11/11.

## Limitations
- `@anthropic-ai/sdk` 1.8.0 sends the API key as `Authorization: Bearer <key>` instead of `x-api-key`. I kept the
  SDK's behaviour because the spec requires the official SDK.
- JavaScript regex syntax differs from other languages' (e.g. there is no inline global `(?i)`). Patterns that
  compile under neither `u` nor plain mode are reported as errors.
- `list_files` and `search` walk the whole tree before sorting and cutting, so very large trees cost time in
  proportion to their size.
- Sorting uses UTF-16 code-unit order. This differs from code-point order only for paths that mix characters
  above U+FFFF with characters U+E000–U+FFFF.
- The shebang uses `env -S` (works on macOS and current GNU coreutils).
- Code caches are specific to the Node/V8 version and flags. They are built by BUILD.md on the target machine;
  if they don't match, V8 rejects them and startup is ~25 ms slower, with the same behaviour.
- The bundles keep the original module paths relative to the workspace, so the workspace can move. Any
  non-literal `require` would still load from `node_modules/` (none happens on tinycoder's paths).
- The files server's caches use up to ~16 MB more memory on large repositories (bounded, then cleared).
- Per-call numbers in the benchmark are dominated by client-side cost that grows with response size (see above).
  A cold `search` (first call, or files changed in the last 2 s) reads every file. That is parallel, but costs
  ~2.5 ms per 350 files here.
- I could not test against a real network endpoint (loopback is blocked here). The fake-API path was tested with an
  in-process `fetch` replacement and through `tests/check.py`.
