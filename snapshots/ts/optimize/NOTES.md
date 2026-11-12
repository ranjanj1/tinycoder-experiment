# tinycoder, lane TS: notes

## Result
`python3 tests/check.py bin/tinycoder` → **11/11 passed** (files_tools_listed, shell_tools_listed, list_and_read,
write_and_edit, search_regex, files_refuse_escapes, shell_tests_and_commands, agent_basic_loop, agent_hook_blocks,
agent_step_limit, agent_retries_overloaded).

I did not read the sources of `tests/` (check.py, fake_api.py, mcp_client.py, task files). I only ran check.py.
I did not run `tests/bench_dev.py`.

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
```
Loopback TCP connections are blocked in this sandbox (even with the sandbox disabled), so I tested the agent loop
by replacing `fetch` in-process instead of with an HTTP fake.

## Layout
- `src/main.ts`: CLI (`serve files`, `serve shell`, `run`) using `node:util` `parseArgs`. It dynamically imports only
  the module the command needs, so the servers never load the Anthropic SDK.
- `src/files.ts`: MCP server `files`. `src/shell.ts`: MCP server `shell`.
- `src/agent.ts`: `run`. It spawns both servers and is an MCP client over stdio. It also holds the hooks, the
  model loop, the runs log and the final JSON.
- `src/allow.ts`: the `run_command` allowlist, used by both the shell server and the pre-call hook.
- `src/result.ts`: tool result helpers and the last-N-characters tail.
- `bin/tinycoder`: `#!/usr/bin/env -S node --max-semi-space-size=1`. It turns on `module.enableCompileCache()`, then
  imports `dist/main.js`.

## Design
- **MCP**: both servers use the SDK's low-level `Server` (`@modelcontextprotocol/sdk/server/index.js`) with
  `setRequestHandler(ListToolsRequestSchema | CallToolRequestSchema)` and `StdioServerTransport`. The agent uses the
  SDK's `Client` and `StdioClientTransport`. I wrote no protocol code myself.
  - I used the low-level `Server` rather than `McpServer` + zod for two reasons. The input schemas can be plain
    JSON Schema exactly as I want them listed (e.g. `minimum: 1`, `minItems: 1`). And every bad-argument case goes
    through my own validation, so the error text always starts with `error: `. McpServer's zod validation errors
    would start with `MCP error -32602`.
- `run` spawns the servers as `process.execPath bin/tinycoder serve …`, with the parent's full environment, so the
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

## Performance notes
- Startup to the `initialize` reply is about 115 ms per server here; bare `node` is about 61 ms. Nearly all of the
  difference is loading the MCP SDK's modules (zod v4 schemas, ajv), and without a bundler that cost stays. Node's
  compile cache saves a few ms on warm starts. Dropping `commander` for `util.parseArgs` saved a little on top.
- `--max-semi-space-size=1` lowers peak RSS per process from about 73 MB to about 66 MB, with no measurable cost
  in time. I tried `--single-threaded` too: together with that flag Node exits before serving stdio, so it is not
  used.
- The files tools use synchronous fs calls. Each server handles one call at a time, so nothing is gained by
  making them async.

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
- I could not test against a real network endpoint (loopback is blocked here). The fake-API path was tested with an
  in-process `fetch` replacement and through `tests/check.py`.
